import os

import pytest

from app.store.memory import InMemoryConfigRepo


async def test_override_roundtrip_memory() -> None:
    repo = InMemoryConfigRepo()
    assert await repo.get_knowledge_override("faq") is None
    await repo.set_knowledge_override("faq", "[]")
    assert await repo.get_knowledge_override("faq") == "[]"
    many = await repo.get_knowledge_overrides(["faq", "business"])
    assert many == {"faq": "[]", "business": None}


async def test_bump_config_int_memory() -> None:
    repo = InMemoryConfigRepo()
    await repo.bump_config_int("knowledge_version")
    assert await repo.get("knowledge_version") == "1"
    await repo.bump_config_int("knowledge_version")
    assert await repo.get("knowledge_version") == "2"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL")
async def test_override_roundtrip_postgres() -> None:
    from app.store.pg_factory import LazyPool
    from app.store.postgres import PostgresConfigRepo

    pool = LazyPool(os.environ["TEST_DATABASE_URL"])
    repo = PostgresConfigRepo(pool)
    await repo.set_knowledge_override("faq", "[]")
    assert await repo.get_knowledge_override("faq") == "[]"
    await repo.set_knowledge_override("faq", "[1]")  # upsert overwrites
    assert await repo.get_knowledge_override("faq") == "[1]"
    await repo.bump_config_int("knowledge_version_test")
    await pool.close()
