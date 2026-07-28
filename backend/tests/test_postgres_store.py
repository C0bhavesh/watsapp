import os
import uuid

import pytest

from app.store.base import MappingUpsert, OutboundDraft
from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresConfigRepo, PostgresIngestStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


def mapping(gid: str) -> MappingUpsert:
    return MappingUpsert(
        order_gid=gid, order_name="tavas1", order_number_int=1,
        phone_e164="+911111111111", customer_name="A", email="a@b.c",
        language="en", financial_status_at_create="PENDING", is_cod=True,
    )


async def test_config_repo_roundtrip_upsert(pool: LazyPool) -> None:
    repo = PostgresConfigRepo(pool)
    key = f"test:{uuid.uuid4()}"
    assert await repo.get(key) is None
    await repo.set(key, "v1")
    await repo.set(key, "v2")
    assert await repo.get(key) == "v2"


async def test_ingest_atomic_and_idempotent(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    gid = f"gid://shopify/Order/{uuid.uuid4()}"
    wh = f"wh-{uuid.uuid4()}"
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164="+911111111111", payload_json="{}",
    )
    first = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (first.duplicate, first.queued) == (False, True)
    again = await store.ingest_order_created(wh, "orders/create", mapping(gid), draft)
    assert (again.duplicate, again.queued) == (True, False)
    other = await store.ingest_order_created(
        f"wh-{uuid.uuid4()}", "orders/create", mapping(gid), draft
    )
    assert (other.duplicate, other.queued) == (False, False)  # dedupe_key already used
