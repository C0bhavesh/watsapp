"""Cancel reconciliation job: apply the final `cancelled` tag once Shopify confirms cancelledAt."""

import httpx
import pytest

from app.core.order_resolver import authorize_own_order
from app.deps import get_container, reset_container
from app.jobs.reconcile import run_reconcile_cancels
from app.shopify.models import AuthorizedOrder, CancelRequested, Order

CRON = "topsecret-reconcile-1"  # >= 16 chars


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    yield
    reset_container()


def _order(
    gid: str, phone: str | None = "+919664290413", cancelled_at: str | None = None
) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class FakeShopify:
    def __init__(self, orders: dict[str, Order]) -> None:
        self.orders = orders
        self.add_tags_calls: list[tuple[str, list[str]]] = []

    async def get_order(self, gid: str) -> Order | None:
        return self.orders.get(gid)

    async def add_tags(self, auth: object, tags: object) -> None:
        self.add_tags_calls.append((auth.order.gid, list(tags)))  # type: ignore[attr-defined]

    async def cancel_order(
        self, auth: object, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> CancelRequested:
        raise NotImplementedError


GID = "gid://shopify/Order/1"


# --- authorize_own_order helper (ADR-004: only order_resolver constructs AuthorizedOrder) ---

def test_authorize_own_order_builds_from_best_phone() -> None:
    auth = authorize_own_order(_order(GID))
    assert isinstance(auth, AuthorizedOrder)
    assert auth.verified_phone == "+919664290413"


def test_authorize_own_order_none_without_phone() -> None:
    assert authorize_own_order(_order(GID, phone=None)) is None


# --- reconcile job ---

async def test_cancelled_order_gets_final_tag() -> None:
    c = get_container()
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert c.ingest._mapping_status[GID] == "cancelled"  # type: ignore[attr-defined]
    assert await c.ingest.orders_awaiting_cancel_reconcile() == []
    action = c.ingest.order_actions[-1]  # type: ignore[attr-defined]
    assert action["action"] == "cancelled"
    assert action["actor_wa_id"] == "system"
    assert action["result"] == "ok"


async def test_not_yet_cancelled_left_untouched() -> None:
    c = get_container()
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at=None)})  # Shopify not cancelled yet
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 0
    assert result["pending"] == 1
    assert shopify.add_tags_calls == []
    assert c.ingest._mapping_status[GID] == "cancel_requested"  # type: ignore[attr-defined]
    assert await c.ingest.orders_awaiting_cancel_reconcile() == [GID]


async def test_missing_order_skipped() -> None:
    c = get_container()
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({})  # get_order returns None
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["skipped"] == 1
    assert shopify.add_tags_calls == []
    assert c.ingest._mapping_status[GID] == "cancel_requested"  # type: ignore[attr-defined]


async def test_cancelled_order_without_phone_skipped() -> None:
    c = get_container()
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, phone=None, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["skipped"] == 1
    assert shopify.add_tags_calls == []


async def test_job_registered_and_runs_via_cron_endpoint() -> None:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/internal/jobs/reconcile_cancels", headers={"X-Cron-Secret": CRON}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job"] == "reconcile_cancels"
    assert body["result"]["checked"] == 0  # fresh container, nothing awaiting
