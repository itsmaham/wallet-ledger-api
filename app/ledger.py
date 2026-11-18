"""The three ledger operations and how they stay correct under concurrency.

Every write is ONE SQL statement (a data-modifying CTE) running in its own
implicit transaction at READ COMMITTED:

* The account row is updated first; that takes the row lock which serialises
  all writes for one user. Writes for different users touch different rows and
  never wait on each other.
* The ledger INSERT selects FROM the account CTE, so it only runs after the
  lock is held (and not at all if a debit's guard failed). The ledger id is
  therefore assigned in the same order the user's writes are serialised.
* A debit's guard `balance >= amount` is re-checked by PostgreSQL against the
  newest committed row after waiting for the lock (READ COMMITTED's
  re-evaluation of the WHERE clause), so two racing debits can never both pass.
* Ledger row and balance change commit or roll back together.

Each statement locks exactly one account row, so deadlocks cannot happen
between these statements; the retry loop is a safety net, not a mechanism.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable

import asyncpg
import orjson

from .errors import ApiError, error_body
from .limiter import KeyedLimiter

Result = tuple[int, dict]
Operation = Callable[[asyncpg.Connection], Awaitable[Result]]

CREDIT_SQL = """
WITH acc AS (
    INSERT INTO accounts AS a (user_id, balance)
    VALUES ($1::text, $2::bigint)
    ON CONFLICT (user_id) DO UPDATE
        SET balance = a.balance + EXCLUDED.balance,
            updated_at = now()
    RETURNING a.balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT $1::text, 'credit'::ledger_entry_type, $2::bigint FROM acc
)
SELECT balance FROM acc
"""

DEBIT_SQL = """
WITH acc AS (
    UPDATE accounts
       SET balance = balance - $2::bigint,
           updated_at = now()
     WHERE user_id = $1::text
       AND balance >= $2::bigint
    RETURNING balance
), entry AS (
    INSERT INTO ledger (user_id, type, amount)
    SELECT $1::text, 'debit'::ledger_entry_type, $2::bigint FROM acc
)
SELECT balance FROM acc
"""

BALANCE_SQL = "SELECT balance FROM accounts WHERE user_id = $1::text"

IDEMPOTENCY_CLAIM_SQL = """
INSERT INTO idempotency_keys (user_id, idem_key, operation, amount)
VALUES ($1::text, $2::text, $3::ledger_entry_type, $4::bigint)
ON CONFLICT DO NOTHING
RETURNING true
"""

IDEMPOTENCY_GET_SQL = """
SELECT operation::text AS operation, amount, status_code, response::text AS response
  FROM idempotency_keys
 WHERE user_id = $1::text AND idem_key = $2::text
"""

IDEMPOTENCY_SAVE_SQL = """
UPDATE idempotency_keys
   SET status_code = $3::smallint, response = $4::jsonb
 WHERE user_id = $1::text AND idem_key = $2::text
"""

# Errors after which PostgreSQL has rolled the transaction back, so running it
# again is safe and cannot double-apply.
RETRYABLE_ERRORS = (asyncpg.SerializationError, asyncpg.DeadlockDetectedError)


def _ok(user_id: str, balance: int) -> Result:
    return 200, {"user_id": user_id, "balance": balance}


def _user_not_found() -> Result:
    return 404, error_body("user_not_found", "user has never been credited")


class Ledger:
    def __init__(self, pool: asyncpg.Pool, limiter: KeyedLimiter, *,
                 max_retries: int, acquire_timeout: float) -> None:
        self._pool = pool
        self._limiter = limiter
        self._max_retries = max_retries
        self._acquire_timeout = acquire_timeout

    # ---- public operations ------------------------------------------------

    async def credit(self, user_id: str, amount: int, idempotency_key: str | None) -> Result:
        async def op(conn: asyncpg.Connection) -> Result:
            balance = await conn.fetchval(CREDIT_SQL, user_id, amount)
            return _ok(user_id, balance)

        return await self._write(user_id, "credit", amount, idempotency_key, op)

    async def debit(self, user_id: str, amount: int, idempotency_key: str | None) -> Result:
        async def op(conn: asyncpg.Connection) -> Result:
            balance = await conn.fetchval(DEBIT_SQL, user_id, amount)
            if balance is not None:
                return _ok(user_id, balance)
            # Nothing was written. Work out why: unknown user or not enough money.
            current = await conn.fetchval(BALANCE_SQL, user_id)
            if current is None:
                return _user_not_found()
            return 409, error_body("insufficient_funds",
                                   f"balance {current} is less than debit amount {amount}")

        return await self._write(user_id, "debit", amount, idempotency_key, op)

    async def balance(self, user_id: str) -> Result:
        # Plain read: no row lock, never blocked by writers (MVCC). It reads the
        # primary, so any write that already returned 200 is visible.
        async with self._pool.acquire(timeout=self._acquire_timeout) as conn:
            balance = await conn.fetchval(BALANCE_SQL, user_id)
        if balance is None:
            return _user_not_found()
        return _ok(user_id, balance)

    async def ping(self) -> None:
        async with self._pool.acquire(timeout=self._acquire_timeout) as conn:
            await conn.fetchval("SELECT 1")

    # ---- plumbing ---------------------------------------------------------

    async def _write(self, user_id: str, operation: str, amount: int,
                     idempotency_key: str | None, op: Operation) -> Result:
        async with self._limiter.hold(user_id):
            attempt = 0
            while True:
                try:
                    async with self._pool.acquire(timeout=self._acquire_timeout) as conn:
                        if idempotency_key is None:
                            return await op(conn)
                        return await self._idempotent(conn, user_id, idempotency_key,
                                                      operation, amount, op)
                except RETRYABLE_ERRORS:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise
                    # Short jittered backoff so retries do not collide again.
                    await asyncio.sleep(random.uniform(0, 0.005 * 2 ** attempt))

    async def _idempotent(self, conn: asyncpg.Connection, user_id: str, key: str,
                          operation: str, amount: int, op: Operation) -> Result:
        async with conn.transaction():
            # A concurrent request with the same key blocks here until the first
            # one commits (or rolls back), then sees its stored response.
            claimed = await conn.fetchval(IDEMPOTENCY_CLAIM_SQL, user_id, key, operation, amount)
            if not claimed:
                stored = await conn.fetchrow(IDEMPOTENCY_GET_SQL, user_id, key)
                if stored["operation"] != operation or stored["amount"] != amount:
                    raise ApiError(422, "idempotency_key_reused",
                                   "Idempotency-Key was already used with a different request")
                return stored["status_code"], orjson.loads(stored["response"])
            status, body = await op(conn)
            await conn.execute(IDEMPOTENCY_SAVE_SQL, user_id, key, status,
                               orjson.dumps(body).decode())
            return status, body
