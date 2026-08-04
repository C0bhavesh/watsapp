"""Vercel getaddrinfo-EBUSY workaround wiring for LazyPool.

Mocks only (socket.getaddrinfo + asyncpg.create_pool) — no real DB. The real
Postgres integration tests stay skipped without TEST_DATABASE_URL. The bug is
Vercel-runtime-specific; these tests validate the connect-kwargs wiring only.
"""

import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest

from app.store.pg_factory import LazyPool

_SUPABASE_DSN = "postgresql://u:p@db.abc.pooler.supabase.com:6543/postgres"
_LOCAL_DSN = "postgresql://u:p@localhost:5432/postgres"
_FAKE_IPV4 = "203.0.113.7"


class _FakePool:
    """Stands in for an asyncpg.Pool; acquire() yields a dummy connection."""

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[object]:
        yield object()

    async def close(self) -> None:
        return None


def _capturing_create_pool(
    captured: dict[str, Any],
) -> Callable[..., Any]:
    async def _stub(dsn: str, **kwargs: Any) -> _FakePool:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _FakePool()

    return _stub


async def test_supabase_host_pins_ipv4_and_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_FAKE_IPV4, 6543))
        ],
    )
    monkeypatch.setattr(asyncpg, "create_pool", _capturing_create_pool(captured))

    pool = LazyPool(_SUPABASE_DSN)
    async with pool.acquire() as conn:
        assert conn is not None

    assert captured["dsn"] == _SUPABASE_DSN
    assert captured["kwargs"]["host"] == _FAKE_IPV4
    assert captured["kwargs"]["ssl"] == "require"
    assert captured["kwargs"]["statement_cache_size"] == 0


async def test_non_supabase_host_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _no_resolve(*a: Any, **k: Any) -> Any:
        raise AssertionError("getaddrinfo must not run for a non-supabase DSN")

    monkeypatch.setattr(socket, "getaddrinfo", _no_resolve)
    monkeypatch.setattr(asyncpg, "create_pool", _capturing_create_pool(captured))

    pool = LazyPool(_LOCAL_DSN)
    async with pool.acquire() as conn:
        assert conn is not None

    assert captured["dsn"] == _LOCAL_DSN
    assert "host" not in captured["kwargs"]
    assert "ssl" not in captured["kwargs"]
    assert captured["kwargs"]["statement_cache_size"] == 0


async def test_supabase_getaddrinfo_oserror_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _ebusy(*a: Any, **k: Any) -> Any:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(socket, "getaddrinfo", _ebusy)
    monkeypatch.setattr(asyncpg, "create_pool", _capturing_create_pool(captured))

    pool = LazyPool(_SUPABASE_DSN)
    async with pool.acquire() as conn:
        assert conn is not None

    assert captured["dsn"] == _SUPABASE_DSN
    assert "host" not in captured["kwargs"]
    assert "ssl" not in captured["kwargs"]
    assert captured["kwargs"]["statement_cache_size"] == 0
