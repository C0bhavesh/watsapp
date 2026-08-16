"""Cancel reconciliation job: apply the final `cancelled` tag once Shopify confirms cancelledAt,
and notify the customer with cod_cancel once that happens."""

import httpx
import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult
from app.core.order_resolver import authorize_own_order
from app.deps import get_container, reset_container
from app.jobs.reconcile import run_reconcile_cancels
from app.shopify.models import AuthorizedOrder, CancelRequested, Customer, Order

CRON = "topsecret-reconcile-1"  # >= 16 chars


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    await save_controls(c.config, AdminControls(send_mode="live"))
    yield
    reset_container()


class FakeSender:
    """Records send_template calls made by reconcile.py's cod_cancel notification."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append({"to": to, "template": template_name, "language": language,
                           "body_params": list(body_params)})
        return SendResult(ok=True, status_code=200, wamid="wamid.cancel", error=None)


def _install_sender(monkeypatch: pytest.MonkeyPatch) -> FakeSender:
    sender = FakeSender()
    monkeypatch.setattr("app.jobs.reconcile.send_template", sender)
    return sender


def _order(
    gid: str, phone: str | None = "+919664290413", cancelled_at: str | None = None,
    customer: Customer | None = None,
) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=cancelled_at, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None, customer=customer,
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


async def test_cancelled_order_sends_cod_cancel_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    customer = Customer(
        gid="gid://shopify/Customer/1", first_name="Suman", last_name="B", email=None, phone=None,
        address_line1=None, address_line2=None, city=None, state=None, postal_code=None,
        country=None,
    )
    shopify = FakeShopify(
        {GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z", customer=customer)}
    )
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert sender.calls == [
        {"to": "+919664290413", "template": "cod_cancel", "language": "en",
         "body_params": ["Suman B", "tavas1"]}
    ]


async def test_reconciled_notification_suppressed_by_send_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    await save_controls(c.config, AdminControls(send_mode="off"))
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # The kill switch affects notification only -- the tag/status write still happens.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert sender.calls == []


async def test_reconcile_survives_corrupt_whatsapp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    # Simulate a master-key rotation / corrupted encrypted secret (same technique used in
    # test_whatsapp_webhook.py's VaultError tests): a raw config_repo write bypasses encryption,
    # so decrypt later raises VaultError.
    await c.config_repo.set("whatsapp:app_secret", "gAAAAAcorrupt")
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # Reconciliation (the primary job) is unaffected by a broken WhatsApp config -- only the
    # notification is skipped.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert sender.calls == []


async def test_reconcile_survives_transport_failure_during_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    c = get_container()

    async def _raising_send_template(*args, **kwargs):
        raise WhatsAppSendError("timed out")

    monkeypatch.setattr("app.jobs.reconcile.send_template", _raising_send_template)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    # The tag/status write already happened before the notification attempt -- a transport
    # failure sending cod_cancel must not undo it or affect the job's own success count.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(GID, ["cancelled"])]
    assert c.ingest._mapping_status[GID] == "cancelled"  # type: ignore[attr-defined]


async def test_release_claim_store_error_does_not_abort_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A store-layer error while RELEASING one order's claim (a non-ShopifyError from the ingest
    # store) must NOT abort the rest of the batch: the remaining claimed orders still get
    # processed. Guards this file's documented "one row never aborts the whole job" contract.
    c = get_container()
    gid2 = "gid://shopify/Order/2"
    await c.ingest.set_mapping_status(GID, "cancel_requested")   # not-yet-cancelled -> releases
    await c.ingest.set_mapping_status(gid2, "cancel_requested")  # cancelled -> reconciled
    shopify = FakeShopify(
        {
            GID: _order(GID, cancelled_at=None),
            gid2: _order(gid2, cancelled_at="2026-08-10T00:00:00Z"),
        }
    )
    c.shopify = shopify  # type: ignore[assignment]

    real_set = c.ingest.set_mapping_status

    async def _flaky_set(order_gid: str, status: str) -> None:
        if order_gid == GID and status == "cancel_requested":
            raise RuntimeError("transient store hiccup")
        await real_set(order_gid, status)

    monkeypatch.setattr(c.ingest, "set_mapping_status", _flaky_set)

    result = await run_reconcile_cancels(c)

    # GID's release raised and was swallowed; GID2 was still reconciled.
    assert result["reconciled"] == 1
    assert shopify.add_tags_calls == [(gid2, ["cancelled"])]
    assert c.ingest._mapping_status[gid2] == "cancelled"  # type: ignore[attr-defined]


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


async def test_notification_routes_to_mapping_phone_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mapping phone (already E.164, and the number that actually held the WhatsApp
    # conversation) is preferred over the order's raw best_phone for the recipient.
    from app.store.base import MappingUpsert

    c = get_container()
    sender = _install_sender(monkeypatch)
    c.ingest.mappings[GID] = MappingUpsert(  # type: ignore[attr-defined]
        order_gid=GID, order_name="tavas1", order_number_int=1, phone_e164="+919812345678",
        customer_name="Suman", email=None, language="en",
        financial_status_at_create="PENDING", is_cod=True,
    )
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify(
        {GID: _order(GID, phone="09664290413", cancelled_at="2026-08-10T00:00:00Z")}
    )
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert result["notified"] == 1
    assert sender.calls[0]["to"] == "+919812345678"  # mapping phone, not the raw order phone


async def test_notification_falls_back_to_normalized_order_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No mapping phone -> fall back to the order's best phone NORMALIZED to E.164 (never the raw
    # Shopify string): a bare "09664290413" must be delivered as "+919664290413".
    c = get_container()
    sender = _install_sender(monkeypatch)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify(
        {GID: _order(GID, phone="09664290413", cancelled_at="2026-08-10T00:00:00Z")}
    )
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert result["notified"] == 1
    assert sender.calls[0]["to"] == "+919664290413"


async def test_notify_counters_track_suppressed_and_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    await save_controls(c.config, AdminControls(send_mode="off"))
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert result["notified"] == 0
    assert result["notify_skipped"] == 1


async def test_failed_send_result_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # A >=400 Meta response returns SendResult(ok=False, ...) rather than raising: it must be
    # logged (with the gid + status/error) and counted as skipped, not silently swallowed.
    import logging

    c = get_container()

    async def _failing_send(*args, **kwargs) -> SendResult:
        return SendResult(ok=False, status_code=429, wamid=None, error="code=131056")

    monkeypatch.setattr("app.jobs.reconcile.send_template", _failing_send)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="app.jobs.reconcile"):
        result = await run_reconcile_cancels(c)

    assert result["reconciled"] == 1
    assert result["notify_skipped"] == 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert GID in joined
    assert "429" in joined


async def test_transport_failure_log_includes_gid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from app.channels.whatsapp_sender import WhatsAppSendError

    c = get_container()

    async def _raising_send_template(*args, **kwargs):
        raise WhatsAppSendError("timed out")

    monkeypatch.setattr("app.jobs.reconcile.send_template", _raising_send_template)
    await c.ingest.set_mapping_status(GID, "cancel_requested")
    shopify = FakeShopify({GID: _order(GID, cancelled_at="2026-08-10T00:00:00Z")})
    c.shopify = shopify  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="app.jobs.reconcile"):
        result = await run_reconcile_cancels(c)

    assert result["notify_skipped"] == 1
    assert GID in " ".join(r.getMessage() for r in caplog.records)


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
