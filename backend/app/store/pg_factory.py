import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


class LazyPool:
    """Pool created on first acquire — never at import time (serverless cold-start rule)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    # statement_cache_size=0: Supabase's transaction-mode pooler (6543)
                    # breaks asyncpg prepared statements; harmless on direct connections.
                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=0, max_size=5, statement_cache_size=0
                    )
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
