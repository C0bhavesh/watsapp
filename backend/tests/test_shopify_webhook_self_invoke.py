"""orders/create self-invoke: the webhook triggers the WhatsApp send itself (Flow A).

The webhook queues an ``outbound_messages`` row, acks Shopify, then sends that ONE row via a
FastAPI BackgroundTask. It must never call the shared, non-atomic ``claim_queued_outbound`` path
(two concurrent orders could both claim one row and double-send) — each order sends only the row
it just created. The ``send_mode`` kill switch is honoured exactly like ``run_outbox_drain``.
"""

import base64
import hashlib
import hmac as hmac_lib
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult
from app.deps import get_container, reset_container
from app.store.base import MappingUpsert, OutboundDraft

SECRET = "csec-self-invoke"
PHONE = "+919664290413"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    c = get_container()
    await c.config.set_secret("shopify:client_secret", SECRET)
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    yield
    reset_container()


class FakeSender:
    """Records each send_template call; returns scripted results (or raises) one per call."""

    def __init__(self, results: list[object] | object) -> None:
        self.calls: list[dict[str, object]] = []
        self._results = results

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), timeout=20.0,
    ) -> SendResult:
        self.calls.append({"to": to, "button_payloads": list(button_payloads)})
        result = self._results.pop(0) if isinstance(self._results, list) else self._results
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, SendResult)
        return result


def _install_sender(monkeypatch: pytest.MonkeyPatch, sender: FakeSender) -> None:
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)


def payload(gid: str = "gid://shopify/Order/1", cod: bool = True) -> dict:
    p = {
        "admin_graphql_api_id": gid,
        "name": "tavas3733",
        "order_number": 3733,
        "email": "c@example.com",
        "customer": {"first_name": "Suman", "last_name": "B"},
        "shipping_address": {"phone": PHONE},
        "tags": "COD" if cod else "online",
        "payment_gateway_names": ["Cash on Delivery (COD)"] if cod else ["Razorpay"],
        "total_price": "949.00",
        "created_at": datetime.now(UTC).isoformat(),
        "customer_locale": "hi-IN",
    }
    return p


def sign(body: bytes) -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


def headers(body: bytes, webhook_id: str = "wh-1") -> dict:
    return {
        "X-Shopify-Hmac-Sha256": sign(body),
        "X-Shopify-Topic": "orders/create",
        "X-Shopify-Webhook-Id": webhook_id,
        "Content-Type": "application/json",
    }


async def post(body: bytes, hdrs: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/shopify", content=body, headers=hdrs)


async def test_eligible_order_sends_the_template_as_a_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Starlette runs BackgroundTasks AFTER the response is constructed (and, over ASGITransport,
    # before .post returns) — so a 200 + a recorded send proves the send ran post-ack, not inline.
    await save_controls(get_container().config, AdminControls(send_mode="live"))
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.1", error=None))
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/1"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    assert [call["to"] for call in sender.calls] == [PHONE]
    assert sender.calls[0]["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    views = {v.dedupe_key: v for v in await get_container().ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "sent"


async def test_two_orders_each_send_only_their_own_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two near-simultaneous orders: each self-invoke sends ONLY the row it created. A pre-existing
    # unrelated queued row is left untouched — proof the path never touches the shared claim queue.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    # An unrelated queued row that neither webhook created (would be sent if the claim path ran).
    await c.ingest.ingest_order_created(
        "wh-pre", "orders/create",
        MappingUpsert(
            order_gid="gid://shopify/Order/pre", order_name="tavas0", order_number_int=1,
            phone_e164=PHONE, customer_name="Pre", email=None, language="hi",
            financial_status_at_create="PENDING", is_cod=True,
        ),
        OutboundDraft(
            dedupe_key="order_created:gid://shopify/Order/pre", kind="order_confirmation",
            phone_e164=PHONE, payload_json=json.dumps(
                {"template": "order_confirmation_cod", "language": "hi",
                 "customer_name": "Pre", "order_name": "tavas0", "amount": "1"}
            ),
        ),
    )
    sender = FakeSender([SendResult(ok=True, status_code=200, wamid="w", error=None)] * 2)
    _install_sender(monkeypatch, sender)

    gid_a = "gid://shopify/Order/A"
    gid_b = "gid://shopify/Order/B"
    body_a = json.dumps(payload(gid_a)).encode()
    body_b = json.dumps(payload(gid_b)).encode()
    await post(body_a, headers(body_a, webhook_id="wh-A"))
    await post(body_b, headers(body_b, webhook_id="wh-B"))

    # Exactly two sends — one per order's own button payloads; the pre-existing row was NOT sent.
    sent_buttons = sorted(call["button_payloads"][0] for call in sender.calls)
    assert sent_buttons == [f"order:confirm:{gid_a}", f"order:confirm:{gid_b}"]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views["order_created:gid://shopify/Order/pre"].state == "queued"


async def test_send_mode_off_triggers_no_send_and_leaves_row_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default controls -> send_mode "off": the kill switch must suppress the send entirely.
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/off"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-off"))

    assert resp.json() == {"ok": True, "duplicate": False, "queued": True}
    assert sender.calls == []
    views = {v.dedupe_key: v for v in await get_container().ingest.recent_outbound(10)}
    # off leaves the row queued (a future backstop drain could send it), same as run_outbox_drain.
    assert views[f"order_created:{gid}"].state == "queued"


async def test_shadow_mode_triggers_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    await save_controls(get_container().config, AdminControls(send_mode="shadow"))
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/shadow"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-shadow"))

    assert resp.status_code == 200
    assert sender.calls == []
    views = {v.dedupe_key: v for v in await get_container().ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "suppressed"


async def test_allowlist_miss_triggers_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    await save_controls(
        get_container().config,
        AdminControls(send_mode="allowlist", allowlist_phones=["+911111111111"]),
    )
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/allow"
    body = json.dumps(payload(gid)).encode()
    await post(body, headers(body, webhook_id="wh-allow"))

    assert sender.calls == []


async def test_duplicate_webhook_triggers_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replayed delivery has outbound_id=None (no fresh row) -> no second send.
    await save_controls(get_container().config, AdminControls(send_mode="live"))
    sender = FakeSender([SendResult(ok=True, status_code=200, wamid="w", error=None)])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/dup"
    body = json.dumps(payload(gid)).encode()
    await post(body, headers(body, webhook_id="wh-dup"))
    await post(body, headers(body, webhook_id="wh-dup"))  # same webhook id -> duplicate

    assert len(sender.calls) == 1


async def test_ineligible_order_triggers_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prepaid order under the default cod_only policy queues nothing -> nothing to self-invoke.
    await save_controls(get_container().config, AdminControls(send_mode="live"))
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/prepaid"
    body = json.dumps(payload(gid, cod=False)).encode()
    resp = await post(body, headers(body, webhook_id="wh-prepaid"))

    assert resp.json() == {"ok": True, "duplicate": False, "queued": False}
    assert sender.calls == []


async def test_transport_error_in_background_task_does_not_crash_and_bumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.whatsapp_sender import WhatsAppSendError

    await save_controls(get_container().config, AdminControls(send_mode="live"))
    sender = FakeSender([WhatsAppSendError("network down")])
    _install_sender(monkeypatch, sender)

    gid = "gid://shopify/Order/transport"
    body = json.dumps(payload(gid)).encode()
    resp = await post(body, headers(body, webhook_id="wh-transport"))

    # The webhook still acked 200, and the row was bumped for a later backstop-drain retry.
    assert resp.status_code == 200
    views = {v.dedupe_key: v for v in await get_container().ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "queued"
    assert view.attempts == 1
