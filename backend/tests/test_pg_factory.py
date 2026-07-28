from app.store.pg_factory import LazyPool


async def test_close_on_never_used_pool_is_noop() -> None:
    p = LazyPool("postgresql://unused/db")
    await p.close()  # no pool was ever created -> must not raise
    await p.close()  # idempotent
