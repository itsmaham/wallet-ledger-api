"""Applies db/migrations/*.sql in order, once, at startup.

Every uvicorn worker calls this; a session advisory lock makes them take turns
so only the first one does any work.
"""

import asyncio
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger("ledger.migrate")

MIGRATION_LOCK_ID = 7_241_001  # arbitrary, just unique to this app


async def connect_with_retry(dsn: str, attempts: int = 60) -> asyncpg.Connection:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.CannotConnectNowError, asyncpg.PostgresConnectionError):
            if attempt == attempts:
                raise
            await asyncio.sleep(1)
    raise AssertionError("unreachable")


async def run_migrations(dsn: str, directory: Path) -> None:
    conn = await connect_with_retry(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in applied:
                    continue
                log.info("applying migration %s", path.name)
                async with conn.transaction():
                    # No arguments -> simple query protocol, so a file may hold
                    # many statements, including function bodies.
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)
    finally:
        await conn.close()
