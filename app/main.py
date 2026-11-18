"""HTTP layer: routes, wiring and error mapping.

Run with: uvicorn app.main:app --port 8080
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from . import config
from .auth import BearerAuthMiddleware
from .errors import ApiError, error_response, json_response
from .ledger import Ledger
from .limiter import KeyedLimiter
from .migrate import run_migrations
from .validation import (
    parse_json_object,
    validate_amount,
    validate_idempotency_key,
    validate_user_id,
)

log = logging.getLogger("ledger")


# ---- routes -----------------------------------------------------------------

async def _parse_write(request: Request) -> tuple[str, int, str | None]:
    body = parse_json_object(await request.body())
    user_id = validate_user_id(body.get("user_id"))
    amount = validate_amount(body.get("amount"))
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    return user_id, amount, key


async def credit(request: Request) -> Response:
    user_id, amount, key = await _parse_write(request)
    status, payload = await request.app.state.ledger.credit(user_id, amount, key)
    return json_response(status, payload)


async def debit(request: Request) -> Response:
    user_id, amount, key = await _parse_write(request)
    status, payload = await request.app.state.ledger.debit(user_id, amount, key)
    return json_response(status, payload)


async def balance(request: Request) -> Response:
    user_id = validate_user_id(request.path_params["user_id"])
    status, payload = await request.app.state.ledger.balance(user_id)
    return json_response(status, payload)


async def health(request: Request) -> Response:
    await request.app.state.ledger.ping()
    return json_response(200, {"status": "ok"})


# ---- error mapping ----------------------------------------------------------

async def handle_api_error(request: Request, exc: ApiError) -> Response:
    return error_response(exc.status, exc.code, exc.message)


async def handle_http_error(request: Request, exc: HTTPException) -> Response:
    codes = {404: "not_found", 405: "method_not_allowed"}
    return error_response(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))


async def handle_unavailable(request: Request, exc: Exception) -> Response:
    # The database is unreachable, the pool is exhausted past its timeout, or a
    # connection died mid-request. For a write the outcome may be unknown, so
    # this is reported as 503 rather than a success or a definite failure.
    log.warning("database unavailable: %s", type(exc).__name__)
    return error_response(503, "service_unavailable", "database temporarily unavailable, retry later")


async def handle_unexpected(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "internal server error")


# ---- wiring -----------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    await run_migrations(config.DATABASE_URL, config.MIGRATIONS_DIR)
    pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        command_timeout=config.DB_COMMAND_TIMEOUT,
        max_inactive_connection_lifetime=300,
        server_settings={"application_name": "wallet-ledger-api", "jit": "off"},
    )
    app.state.ledger = Ledger(
        pool,
        KeyedLimiter(config.PER_USER_WRITE_CONCURRENCY),
        max_retries=config.DB_MAX_RETRIES,
        acquire_timeout=config.DB_ACQUIRE_TIMEOUT,
    )
    try:
        yield
    finally:
        # Uvicorn stops accepting connections and drains in-flight requests on
        # SIGTERM before this runs, so closing the pool here is graceful.
        await pool.close()


def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/credit", credit, methods=["POST"]),
            Route("/debit", debit, methods=["POST"]),
            Route("/balance/{user_id}", balance, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ],
        middleware=[Middleware(BearerAuthMiddleware, token=config.api_token())],
        exception_handlers={
            ApiError: handle_api_error,
            HTTPException: handle_http_error,
            asyncpg.PostgresConnectionError: handle_unavailable,
            asyncpg.InterfaceError: handle_unavailable,
            asyncpg.CannotConnectNowError: handle_unavailable,
            asyncpg.TooManyConnectionsError: handle_unavailable,
            # Only reached if retries are exhausted, which the single-row
            # locking design should never cause; still never a 500.
            asyncpg.SerializationError: handle_unavailable,
            asyncpg.DeadlockDetectedError: handle_unavailable,
            OSError: handle_unavailable,
            asyncio.TimeoutError: handle_unavailable,
            Exception: handle_unexpected,
        },
        lifespan=lifespan,
    )


app = create_app()
