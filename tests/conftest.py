"""Shared fixtures. Tests run against the live stack (see docker-compose.yml)."""

import os
import time
import uuid

import asyncpg
import httpx
import pytest

API_URL = os.environ.get("API_URL", "http://localhost:8080")
API_TOKEN = os.environ.get("API_TOKEN", "recruitment-test-token")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ledger:ledger@localhost:55432/ledger")
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture(scope="session", autouse=True)
def api_ready() -> None:
    """Wait for the API (it runs migrations on startup)."""
    deadline = time.monotonic() + 90
    while True:
        try:
            if httpx.get(f"{API_URL}/health", headers=AUTH, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"API at {API_URL} did not become healthy")
        time.sleep(1)


@pytest.fixture
async def client():
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=API_URL, headers=AUTH, timeout=60, limits=limits) as c:
        yield c


@pytest.fixture
async def anon():
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as c:
        yield c


@pytest.fixture
async def db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()


def new_user() -> str:
    return "t_" + uuid.uuid4().hex[:24]


@pytest.fixture
def uid() -> str:
    return new_user()


async def ledger_balance(db: asyncpg.Connection, user_id: str) -> int | None:
    return await db.fetchval(
        "SELECT SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END) FROM ledger WHERE user_id = $1",
        user_id,
    )


async def account_balance(db: asyncpg.Connection, user_id: str) -> int | None:
    return await db.fetchval("SELECT balance FROM accounts WHERE user_id = $1", user_id)


async def min_running_balance(db: asyncpg.Connection, user_ids: list[str]) -> int | None:
    return await db.fetchval(
        """
        SELECT min(running) FROM (
            SELECT SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END)
                   OVER (PARTITION BY user_id ORDER BY id) AS running
              FROM ledger WHERE user_id = ANY($1::text[])) r
        """,
        user_ids,
    )
