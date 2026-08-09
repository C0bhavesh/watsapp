"""Outbox drain job: send_mode kill switch, per-mode send/suppress, Meta code taxonomy."""

import json

import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult, WhatsAppSendError
from app.deps import get_container, reset_container
from app.jobs.outbox_drain import run_outbox_drain
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919664290413"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", "sec")
    await c.config.set_secret("whatsapp:verify_token", "ver")
    await c.config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    yield
    reset_container()


class FakeSender:
    """Records send_template calls; returns scripted results (or raises) one per call."""

    def __init__(self, results: list[object] | object) -> None:
        self.calls: list[dict[str, object]] = []
        self._results = results

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                "body_params": list(body_params),
                "button_payloads": list(button_payloads),
            }
        )
        result = self._results.pop(0) if isinstance(self._results, list) else self._results
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, SendResult)
        return result


async def _seed_row(gid: str, phone: str = PHONE, payload: dict[str, str] | None = None,
                    dedupe_key: str | None = None) -> None:
    c = get_container()
    payload = payload or {
        "template": "order_confirmation_cod", "language": "hi",
        "customer_name": "Suman", "order_name": "tavas3733", "amount": "949",
    }
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavas3733", order_number_int=3733, phone_e164=phone,
        customer_name="Suman", email=None, language="hi",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=dedupe_key or f"order_created:{gid}", kind="order_confirmation",
        phone_e164=phone, payload_json=json.dumps(payload),
    )
    await c.ingest.ingest_order_created(f"wh-{gid}", "orders/create", mapping, draft)


def _install_sender(monkeypatch: pytest.MonkeyPatch, sender: FakeSender) -> None:
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)


async def test_send_mode_off_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await _seed_row("gid://shopify/Order/1")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    result = await run_outbox_drain(c)  # default controls -> send_mode "off"
    assert result == {"drained": 0, "sent": 0, "suppressed": 0, "reason": "send_mode off"}
    assert sender.calls == []


async def test_not_configured_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    # Wipe one required whatsapp key so load_whatsapp_config returns None.
    await c.config.set_plain("whatsapp:phone_number_id", "")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    result = await run_outbox_drain(c)
    assert result == {"drained": 0, "error": "whatsapp not configured"}
    assert sender.calls == []


async def test_live_sends_marks_sent_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.1", error=None))
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result == {"drained": 1, "sent": 1, "suppressed": 0, "failed": 0, "undeliverable": 0}
    call = sender.calls[0]
    assert call["to"] == PHONE
    assert call["template"] == "order_confirmation_cod"
    assert call["language"] == "hi"
    assert call["body_params"] == ["Suman", "tavas3733", "949"]
    assert call["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "sent"
    mappings = {m.order_gid: m for m in await c.ingest.recent_mappings(10)}
    assert mappings[gid].status == "template_sent"
    assert not await c.ingest.claim_queued_outbound()


async def test_shadow_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="shadow"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result == {"drained": 1, "sent": 0, "suppressed": 1, "failed": 0, "undeliverable": 0}
    assert sender.calls == []
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "suppressed"


async def test_allowlist_miss_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(
        c.config, AdminControls(send_mode="allowlist", allowlist_phones=["+911111111111"])
    )
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["suppressed"] == 1
    assert sender.calls == []


async def test_undeliverable_meta_code(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(
        SendResult(ok=False, status_code=400, wamid=None, error="code=131026; message=undeliv")
    )
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["undeliverable"] == 1
    assert result["failed"] == 0
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "undeliverable"
    assert view.last_error_code == "131026"


async def test_retryable_code_bumps_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(SendResult(ok=False, status_code=503, wamid=None, error=None))
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["failed"] == 0  # not yet at cap -> stays queued
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "queued"
    assert view.attempts == 1
    assert view.last_error_code == "503"


async def test_bad_dedupe_key_fails_others_still_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    good_gid = "gid://shopify/Order/2"
    await _seed_row("bad", dedupe_key="garbage-no-gid")
    await _seed_row(good_gid)
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.ok", error=None))
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["failed"] == 1
    assert result["sent"] == 1
    # Only the good row was ever sent (the bad row never reached the sender).
    assert [call["to"] for call in sender.calls] == [PHONE]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views["garbage-no-gid"].state == "failed"
    assert views[f"order_created:{good_gid}"].state == "sent"


async def test_transport_error_bumps_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    first_gid = "gid://shopify/Order/1"
    second_gid = "gid://shopify/Order/2"
    await _seed_row(first_gid)
    await _seed_row(second_gid)
    sender = FakeSender(
        [
            WhatsAppSendError("network down"),
            SendResult(ok=True, status_code=200, wamid="wamid.2", error=None),
        ]
    )
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    # The transport error on row 1 did not abort the drain; row 2 still sent.
    assert result["drained"] == 2
    assert result["sent"] == 1
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{first_gid}"].state == "queued"
    assert views[f"order_created:{first_gid}"].attempts == 1
    assert views[f"order_created:{second_gid}"].state == "sent"
