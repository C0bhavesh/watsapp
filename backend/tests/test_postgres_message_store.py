import os
import uuid

import pytest

from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresMessageStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


async def test_record_if_new_then_duplicate(pool: LazyPool) -> None:
    store = PostgresMessageStore(pool)
    message_id = f"wamid.{uuid.uuid4()}"
    assert await store.record_if_new(message_id) is True
    assert await store.record_if_new(message_id) is False
