import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import asyncpg

# Explicit per-statement timeout on the shared pool (project rule: every external call is bounded).
# This pool is shared with the Meta/Shopify webhook edge, which must ack well under 5s, so a single
# hung/pathological query (e.g. a runaway admin-list page) can never pin a connection indefinitely
# and starve the live reply/webhook path. Generous enough for legitimate mirror/backfill reads.
_COMMAND_TIMEOUT_SECONDS = 10.0


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
                    host = urlsplit(self._dsn).hostname
                    if host is not None and "supabase.com" in host:
                        self._pool = await self._create_supabase_pool(host)
                    else:
                        # statement_cache_size=0: Supabase's transaction-mode pooler
                        # (6543) breaks asyncpg prepared statements; harmless on direct
                        # connections.
                        self._pool = await asyncpg.create_pool(
                            self._dsn, min_size=0, max_size=5, statement_cache_size=0,
                            command_timeout=_COMMAND_TIMEOUT_SECONDS,
                        )
        return self._pool

    async def _create_supabase_pool(self, host: str) -> asyncpg.Pool:
        # Vercel's Python runtime raises OSError [Errno 16] EBUSY from asyncio's
        # threaded dual-stack (AF_UNSPEC) getaddrinfo. Resolve IPv4 synchronously and
        # connect by the resolved IP (ssl="require" because connecting by IP skips
        # hostname verification). On resolution failure, fall back to the plain DSN path
        # so we never hard-fail worse than before.
        # statement_cache_size=0: Supabase's transaction-mode pooler (6543) breaks
        # asyncpg prepared statements; harmless on direct connections.
        try:
            ipv4 = socket.getaddrinfo(
                host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
            )[0][4][0]
        except OSError:
            return await asyncpg.create_pool(
                self._dsn, min_size=0, max_size=5, statement_cache_size=0,
                command_timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        return await asyncpg.create_pool(
            self._dsn,
            host=ipv4,
            ssl="require",
            min_size=0,
            max_size=5,
            statement_cache_size=0,
            command_timeout=_COMMAND_TIMEOUT_SECONDS,
        )

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
