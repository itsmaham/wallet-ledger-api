"""Runtime settings, all read from the environment."""

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ledger:ledger@db:5432/ledger")
MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).parent.parent / "db" / "migrations"))

# Connections per uvicorn worker. workers x DB_POOL_MAX must stay below the
# server's max_connections (see docker-compose.yml).
DB_POOL_MIN = _int("DB_POOL_MIN", 5)
DB_POOL_MAX = _int("DB_POOL_MAX", 20)
DB_ACQUIRE_TIMEOUT = float(os.environ.get("DB_ACQUIRE_TIMEOUT", "5"))
DB_COMMAND_TIMEOUT = float(os.environ.get("DB_COMMAND_TIMEOUT", "10"))

# How many writes for the same user one worker lets through to the database at
# once. Writes to one user serialise on its account row anyway, so extra
# in-flight requests would only sit holding pool connections that other users
# need. Excess requests wait in memory instead.
PER_USER_WRITE_CONCURRENCY = _int("PER_USER_WRITE_CONCURRENCY", 2)

# Retries for transient conflicts (serialization failure / deadlock).
DB_MAX_RETRIES = _int("DB_MAX_RETRIES", 5)


def api_token() -> bytes:
    """The bearer token. Refuse to start without one rather than run open."""
    token = os.environ.get("API_TOKEN", "")
    if not token:
        raise RuntimeError("API_TOKEN must be set")
    return token.encode("utf-8")
