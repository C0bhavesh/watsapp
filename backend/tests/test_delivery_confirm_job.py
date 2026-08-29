"""Delivery-confirmation sweep job (RTO-aware `order_delivered` gate).

The `delivered` webhook parks a `pending_delivery_confirmations` row due ~2h later instead of
sending order_delivered immediately (Delhivery stamps "delivered" on an RTO's return-to-origin scan
too). This job sweeps the due rows and decides for real: ad2ship first (reliable RTO signal), then a
leaky Shopify heuristic only when ad2ship can't be read. Only a confirmed genuine delivery sends.
"""

import httpx
import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult
from app.deps import get_container, reset_container
from app.jobs.delivery_confirm import run_delivery_confirm
from app.shopify.ad2ship import Ad2shipTracking
from app.shopify.models import Fulfillment, FulfillmentEvent, Order

PHONE = "+919664290413"
CRON = "topsecret-delivery-1"  # >= 16 chars
ORDER_GID = "gid://shopify/Order/1"
FUL_GID = "gid://shopify/Fulfillment/1"
DEDUPE = f"fulfillment_delivered:{FUL_GID}"


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
    """Records send_template calls made by the inline fulfillment send."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {"to": to, "template": template_name, "language": language,
             "body_params": list(body_params)}
        )
        return SendResult(ok=True, status_code=200, wamid="wamid.delivered", error=None)


class FakeShopify:
    def __init__(self, fulfillments: tuple[Fulfillment, ...] = ()) -> None:
        self._fulfillments = fulfillments

    async def get_order_fulfillments(self, gid: str) -> tuple[Fulfillment, ...]:
        return self._fulfillments


def _install_sender(monkeypatch: pytest.MonkeyPatch) -> FakeSender:
    sender = FakeSender()
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)
    return sender


def _install_ad2ship(
    monkeypatch: pytest.MonkeyPatch, result: Ad2shipTracking | None
) -> None:
    async def _stub(http, awb, *, timeout: float = 4.0) -> Ad2shipTracking | None:
        return result

    monkeypatch.setattr("app.jobs.delivery_confirm.fetch_tracking", _stub)


def _tracking(
    status: str, *, current_city: str | None = None, current_hub: str | None = None
) -> Ad2shipTracking:
    return Ad2shipTracking(
        status=status, status_label=status.replace("_", " ").title(),
        current_city=current_city, current_hub=current_hub,
        last_scan="Delivered", last_scan_remark="Delivered to consignee",
        last_scan_at="2026-08-29 10:00", expected_date="2026-08-30",
    )


def _order() -> Order:
    return Order(
        gid=ORDER_GID, name="tavas3908", email=None, phone=PHONE, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


def _fulfillment(
    *, display_status: str | None = None, events: tuple[FulfillmentEvent, ...] = ()
) -> Fulfillment:
    return Fulfillment(
        gid=FUL_GID, status=None, tracking_company="Delhivery",
        tracking_number="AWB123", tracking_url=None,
        display_status=display_status, events=events,
    )


async def _seed(*, due_minutes_ago: int = 1) -> None:
    from datetime import UTC, datetime, timedelta

    c = get_container()
    await c.ingest.upsert_order_mirror(_order())
    await c.ingest.upsert_fulfillment(ORDER_GID, _fulfillment())
    await c.ingest.record_pending_delivery_confirmation(
        fulfillment_gid=FUL_GID, order_gid=ORDER_GID, phone_e164=PHONE,
        due_at=datetime.now(UTC) - timedelta(minutes=due_minutes_ago),
    )


async def _stored_fulfillment() -> Fulfillment:
    c = get_container()
    order = await c.ingest.get_mirrored_order(ORDER_GID)
    assert order is not None
    return next(f for f in order.fulfillments if f.gid == FUL_GID)


async def _outbound_rows(dedupe_key: str) -> list[object]:
    c = get_container()
    return [v for v in await c.ingest.recent_outbound(20) if v.dedupe_key == dedupe_key]


def _confirmation_state(fulfillment_gid: str) -> str | None:
    c = get_container()
    row = c.ingest._pending_confirmations.get(fulfillment_gid)  # type: ignore[attr-defined]
    return row.state if row is not None else None


async def test_ad2ship_delivered_sends_and_marks_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, _tracking("delivered", current_city="Mumbai"))
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["sent"] == 1
    assert result["swept"] == 1
    rows = await _outbound_rows(DEDUPE)
    assert len(rows) == 1
    assert (await _stored_fulfillment()).shipment_status == "delivered"


async def test_ad2ship_rto_records_rto_and_never_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, _tracking("rto_delivered"))
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["rto"] == 1
    assert result["sent"] == 0
    assert await _outbound_rows(DEDUPE) == []
    assert (await _stored_fulfillment()).shipment_status == "rto"


async def test_ad2ship_none_shopify_delivered_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, None)
    c.shopify = FakeShopify(  # type: ignore[assignment]
        (
            _fulfillment(
                display_status="DELIVERED",
                events=(
                    FulfillmentEvent(status="IN_TRANSIT", happened_at="2026-08-28T00:00:00Z"),
                    FulfillmentEvent(status="DELIVERED", happened_at="2026-08-29T00:00:00Z"),
                ),
            ),
        )
    )
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["sent"] == 1
    assert len(await _outbound_rows(DEDUPE)) == 1


async def test_ad2ship_none_shopify_not_delivered_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, None)
    c.shopify = FakeShopify(  # type: ignore[assignment]
        (_fulfillment(display_status="ATTEMPTED_DELIVERY"),)
    )
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["pending"] == 1
    assert result["sent"] == 0
    assert await _outbound_rows(DEDUPE) == []
    # Still due on a later sweep -> row was left pending, not consumed.
    still_due = await c.ingest.due_delivery_confirmations(datetime.now(UTC))
    assert [r.fulfillment_gid for r in still_due] == [FUL_GID]


async def test_ad2ship_non_terminal_writes_snapshot_but_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(
        monkeypatch, _tracking("in_transit", current_city="Pune", current_hub="Hadapsar")
    )
    c.shopify = FakeShopify(  # type: ignore[assignment]
        (_fulfillment(display_status="IN_TRANSIT"),)
    )
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["pending"] == 1
    assert result["sent"] == 0
    assert await _outbound_rows(DEDUPE) == []
    stored = await _stored_fulfillment()
    assert stored.shipment_status == "in_transit"
    assert stored.tracking_city == "Pune"
    assert stored.tracking_hub == "Hadapsar"
    assert stored.tracking_checked_at is not None  # snapshot timestamp written (brief listed it)


async def test_shopify_fallback_unreadable_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, None)
    c.shopify = FakeShopify(())  # type: ignore[assignment]  # empty -> scope/outage simulation
    await _seed()

    result = await run_delivery_confirm(c)

    assert result["pending"] == 1
    assert result["sent"] == 0
    assert result["errors"] == 0
    assert await _outbound_rows(DEDUPE) == []


async def test_one_row_failure_does_not_abort_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Per-row failure isolation: a poison row (row 1's ad2ship fetch raises) must NOT stop row 2
    # from being processed normally, must count as errors==1, and must be left pending (re-swept).
    from datetime import UTC, datetime, timedelta

    c = get_container()
    _install_sender(monkeypatch)
    order2_gid = "gid://shopify/Order/2"
    ful2_gid = "gid://shopify/Fulfillment/2"
    dedupe2 = f"fulfillment_delivered:{ful2_gid}"
    due_at = datetime.now(UTC) - timedelta(minutes=1)

    # Row 1 (poison): default order/fulfillment, AWB123.
    await c.ingest.upsert_order_mirror(_order())
    await c.ingest.upsert_fulfillment(ORDER_GID, _fulfillment())
    await c.ingest.record_pending_delivery_confirmation(
        fulfillment_gid=FUL_GID, order_gid=ORDER_GID, phone_e164=PHONE, due_at=due_at
    )
    # Row 2 (healthy): distinct order/fulfillment, AWB999.
    order2 = Order(
        gid=order2_gid, name="tavas4000", email=None, phone=PHONE, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None, customer_locale=None,
    )
    await c.ingest.upsert_order_mirror(order2)
    await c.ingest.upsert_fulfillment(
        order2_gid,
        Fulfillment(
            gid=ful2_gid, status=None, tracking_company="Delhivery",
            tracking_number="AWB999", tracking_url=None,
        ),
    )
    await c.ingest.record_pending_delivery_confirmation(
        fulfillment_gid=ful2_gid, order_gid=order2_gid, phone_e164=PHONE, due_at=due_at
    )

    async def _stub(http, awb: str, *, timeout: float = 4.0) -> Ad2shipTracking | None:
        if awb == "AWB123":
            raise RuntimeError("ad2ship blew up for row 1")
        return _tracking("delivered")

    monkeypatch.setattr("app.jobs.delivery_confirm.fetch_tracking", _stub)

    result = await run_delivery_confirm(c)

    assert result["swept"] == 2
    assert result["errors"] == 1  # row 1 failed and was isolated
    assert result["sent"] == 1  # row 2 still processed normally
    assert len(await _outbound_rows(dedupe2)) == 1  # row 2's notification went out
    assert await _outbound_rows(DEDUPE) == []  # row 1 sent nothing
    still_due = [
        r.fulfillment_gid for r in await c.ingest.due_delivery_confirmations(datetime.now(UTC))
    ]
    assert FUL_GID in still_due  # row 1 left pending -> retried next run
    assert ful2_gid not in still_due  # row 2 consumed (state advanced to sent)


async def test_row_past_abandon_window_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, None)
    await _seed(due_minutes_ago=8 * 24 * 60)  # 8 days ago -> past _ABANDON_AFTER (7 days)

    result = await run_delivery_confirm(c)

    assert result["abandoned"] == 1
    assert result["sent"] == 0
    assert sender.calls == []
    assert await _outbound_rows(DEDUPE) == []


async def test_second_run_does_not_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    sender = _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, _tracking("delivered"))
    await _seed()

    first = await run_delivery_confirm(c)
    second = await run_delivery_confirm(c)

    assert first["sent"] == 1
    assert second["swept"] == 0  # row now state=sent, no longer due
    assert len(await _outbound_rows(DEDUPE)) == 1  # exactly one outbound row total
    assert len(sender.calls) == 1  # the WhatsApp send fired exactly once across both runs


async def test_preexisting_outbound_row_still_marks_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A durable fulfillment_delivered:{gid} row already exists (a re-swept run / stray earlier
    # enqueue). enqueue_outbound's ON CONFLICT DO NOTHING RETURNING id yields None on that conflict
    # -- which is confirmation the row is already there, NOT a failure. The sweep must still advance
    # the confirmation row to state="sent" (not re-count it errors forever), with no duplicate row.
    import json

    from app.store.base import OutboundDraft

    c = get_container()
    _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, _tracking("delivered"))
    await _seed()
    pre_id = await c.ingest.enqueue_outbound(
        OutboundDraft(
            dedupe_key=DEDUPE, kind="fulfillment_delivered", phone_e164=PHONE,
            payload_json=json.dumps(
                {"template": "order_delivered", "language": "en",
                 "body_params": ["X", "tavas3908"]}
            ),
        )
    )
    assert pre_id is not None  # the pre-existing durable row

    result = await run_delivery_confirm(c)

    assert result["sent"] == 1
    assert result["errors"] == 0
    assert _confirmation_state(FUL_GID) == "sent"
    assert len(await _outbound_rows(DEDUPE)) == 1  # dedupe held -> no duplicate row


async def test_kill_switch_suppresses_actual_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="off"))
    sender = _install_sender(monkeypatch)
    _install_ad2ship(monkeypatch, _tracking("delivered"))
    await _seed()

    result = await run_delivery_confirm(c)

    # Kill switch is enforced at the WhatsApp-call layer, NOT at enqueue: the outbound row IS
    # created (durable, queued for later delivery), so zero Meta calls happen now but the
    # confirmation is genuinely queued -> the job counts it sent and advances the confirmation row
    # to "sent" (it is not lost, just not yet on the wire).
    assert sender.calls == []
    assert result["sent"] == 1
    rows = await _outbound_rows(DEDUPE)
    assert len(rows) == 1
    assert rows[0].state == "queued"  # type: ignore[attr-defined]
    assert _confirmation_state(FUL_GID) == "sent"


def test_delivery_confirm_registered_in_jobs() -> None:
    from app.jobs.router import JOBS

    assert JOBS["delivery_confirm"] is run_delivery_confirm


async def test_job_runs_via_cron_endpoint() -> None:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/internal/jobs/delivery_confirm", headers={"X-Cron-Secret": CRON}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job"] == "delivery_confirm"
    assert body["result"]["swept"] == 0  # fresh container, nothing due
