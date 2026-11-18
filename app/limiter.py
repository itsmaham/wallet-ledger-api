"""Per-key concurrency limit, so one hot user cannot take every DB connection."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedLimiter:
    """At most `permits` concurrent holders per key, in this process.

    Entries are created on first use and dropped when the last holder leaves,
    so memory is bounded by the number of users with requests in flight.
    Safe without locks because everything runs on one event loop.
    """

    def __init__(self, permits: int) -> None:
        self._permits = permits
        self._entries: dict[str, tuple[asyncio.Semaphore, list[int]]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = (asyncio.Semaphore(self._permits), [0])
            self._entries[key] = entry
        semaphore, users = entry
        users[0] += 1
        try:
            async with semaphore:
                yield
        finally:
            users[0] -= 1
            if users[0] == 0:
                del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
