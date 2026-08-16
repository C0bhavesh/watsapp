"""Outbox drain job: send_mode kill switch, per-mode send/suppress, Meta code taxonomy."""

import json
import logging

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
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {
                "to": to,
                "template": template_name,
                "language": language,
                "body_params": dict(body_params),
                "button_payloads": list(button_payloads),
                "header_image_url": header_image_url,
                "timeout": timeout,
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
        "template": "cod_confirmation", "language": "en",
        "customer_name": "Suman", "order_id": "tavas3733",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
        "image_url": "https://cdn.shopify.com/s/files/1/x.jpg",
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


async def test_send_one_outbound_sends_and_transitions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared per-row helper (reused by both run_outbox_drain and the orders/create self-invoke)
    # runs the full state machine for ONE claimed row and reports its outcome.
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.1", error=None))
    _install_sender(monkeypatch, sender)

    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "sent"
    mappings = {m.order_gid: m for m in await c.ingest.recent_mappings(10)}
    assert mappings[gid].status == "template_sent"


async def test_prepaid_order_row_sends_with_no_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/2"
    await _seed_row(gid, payload={
        "template": "prepaid_order", "language": "en",
        "customer_name": "Suman", "order_id": "tavas3734",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    })
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.2", error=None))
    _install_sender(monkeypatch, sender)

    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    outcome = await send_one_outbound(c, cfg, controls, row)

    assert outcome == "sent"
    assert sender.calls[0]["template"] == "prepaid_order"
    assert sender.calls[0]["button_payloads"] == []


async def test_send_one_outbound_default_timeout_is_the_send_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cron-drain path passes NO timeout, so send_template's own 20s default applies — a cron
    # invocation has no <5s ack constraint. This pins that the added optional `timeout` param does
    # not alter the default (cron) behaviour when omitted.
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    await _seed_row("gid://shopify/Order/1")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    await send_one_outbound(c, cfg, controls, row)

    assert sender.calls[0]["timeout"] == 20.0


async def test_send_one_outbound_inline_uses_a_short_distinct_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The inline path (send_inline_outbound) passes the SHORT _INLINE_SEND_TIMEOUT_SECONDS so the
    # Shopify webhook it runs inside stays under the <5s ack budget — distinct from the cron 20s.
    from app.admin.controls import load_controls
    from app.channels.whatsapp_config import load_whatsapp_config
    from app.jobs.outbox_drain import _INLINE_SEND_TIMEOUT_SECONDS, send_one_outbound

    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    await _seed_row("gid://shopify/Order/1")
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)
    controls = await load_controls(c.config)
    cfg = await load_whatsapp_config(c.config)
    assert cfg is not None
    (row,) = await c.ingest.claim_queued_outbound()

    await send_one_outbound(c, cfg, controls, row, timeout=_INLINE_SEND_TIMEOUT_SECONDS)

    assert sender.calls[0]["timeout"] == _INLINE_SEND_TIMEOUT_SECONDS
    assert _INLINE_SEND_TIMEOUT_SECONDS < 20.0  # genuinely shorter than the cron default


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
    assert call["template"] == "cod_confirmation"
    assert call["language"] == "en"
    assert call["body_params"] == {
        "customer_name": "Suman", "order_id": "tavas3733",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    }
    assert call["header_image_url"] == "https://cdn.shopify.com/s/files/1/x.jpg"
    assert call["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "sent"
    mappings = {m.order_gid: m for m in await c.ingest.recent_mappings(10)}
    assert mappings[gid].status == "template_sent"
    assert not await c.ingest.claim_queued_outbound()


async def test_payload_without_image_url_sends_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Q19a graceful degradation: when the ingest-time image fetch failed, no image_url is stored,
    # so the send goes out with header_image_url=None (no header) rather than being blocked.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/9"
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10",
    })
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["sent"] == 1
    assert sender.calls[0]["header_image_url"] is None


async def test_non_https_image_url_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Meta rejects a non-https image link; a stored non-https value must degrade to no header,
    # never be forwarded (defence-in-depth alongside the client's own https check).
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/8"
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10", "image_url": "http://insecure/x.jpg",
    })
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="w", error=None))
    _install_sender(monkeypatch, sender)

    await run_outbox_drain(c)

    assert sender.calls[0]["header_image_url"] is None


async def test_legacy_old_shape_payload_is_undeliverable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A row queued under the OLD template shape (order_confirmation_cod / order_name / amount, no
    # named product fields) can no longer render the new template -> terminal undeliverable rather
    # than an infinite retry (the old template no longer exists on the WABA regardless).
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/7"
    await _seed_row(gid, payload={
        "template": "order_confirmation_cod", "language": "hi",
        "customer_name": "Suman", "order_name": "tavas3733", "amount": "949",
    })
    sender = FakeSender([])
    _install_sender(monkeypatch, sender)

    caplog.set_level(logging.WARNING, logger="app.jobs.outbox_drain")
    result = await run_outbox_drain(c)

    assert result["undeliverable"] == 1
    assert sender.calls == []
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "undeliverable"
    # Observability: a bad/legacy payload marked undeliverable is logged (row id only, no PII) so a
    # post-deploy undeliverable spike is grep-able in Vercel logs.
    undeliverable_logs = [
        r for r in caplog.records
        if "undeliverable" in r.getMessage() and "payload" in r.getMessage()
    ]
    assert len(undeliverable_logs) == 1


async def test_media_error_131052_retries_image_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Q19a: a Shopify/image hiccup must never block the confirmation. When Meta rejects the whole
    # send because the header image URL can't be fetched (131052 -- e.g. the merchant deleted the
    # product photo before a >=1h-later reminder replay), retry the SAME row immediately WITHOUT the
    # image rather than counting it as a failed attempt. The customer still gets the message.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender([
        SendResult(ok=False, status_code=400, wamid=None, error="code=131052; message=media"),
        SendResult(ok=True, status_code=200, wamid="wamid.ok", error=None),
    ])
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["sent"] == 1
    assert result["failed"] == 0
    # First attempt carried the image; the retry stripped it (image-less send).
    assert sender.calls[0]["header_image_url"] == "https://cdn.shopify.com/s/files/1/x.jpg"
    assert sender.calls[1]["header_image_url"] is None
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "sent"
    assert view.attempts == 0  # the media retry did NOT burn an attempt


async def test_media_error_131053_retries_image_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/2"
    await _seed_row(gid)
    sender = FakeSender([
        SendResult(ok=False, status_code=400, wamid=None, error="code=131053; message=media"),
        SendResult(ok=True, status_code=200, wamid="wamid.ok", error=None),
    ])
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["sent"] == 1
    assert sender.calls[1]["header_image_url"] is None


async def test_media_error_without_image_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard against an infinite/pointless retry loop: a media-error code on a row that had NO image
    # to begin with must flow straight through the normal failure classification, not re-send.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/3"
    await _seed_row(gid, payload={
        "template": "cod_confirmation", "language": "en", "customer_name": "A",
        "order_id": "tavas1", "product_name": "P", "product_color": "C",
        "product_size": "S", "product_amount": "10",
    })
    sender = FakeSender([
        SendResult(ok=False, status_code=400, wamid=None, error="code=131052; message=media"),
    ])
    _install_sender(monkeypatch, sender)

    await run_outbox_drain(c)

    assert len(sender.calls) == 1  # no image -> no image-less retry
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "queued"  # normal retryable bump
    assert view.attempts == 1


async def test_media_error_retry_still_failing_bumps_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the image-less retry ALSO fails (with a non-media, non-terminal error), it counts as one
    # normal bumped attempt -- proving the retry result flows through the standard classification
    # and the row is not stuck sending forever.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/4"
    await _seed_row(gid)
    sender = FakeSender([
        SendResult(ok=False, status_code=400, wamid=None, error="code=131052; message=media"),
        SendResult(ok=False, status_code=503, wamid=None, error=None),
    ])
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["sent"] == 0
    assert len(sender.calls) == 2
    assert sender.calls[1]["header_image_url"] is None
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "queued"
    assert view.attempts == 1
    assert view.last_error_code == "503"


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


async def test_error_subcode_not_misread_as_undeliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An error string carrying only `error_subcode=131026` (no TOP-LEVEL `code=`) must NOT be
    # classified undeliverable off a substring match inside `error_subcode` -> it retries.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(
        SendResult(
            ok=False, status_code=400, wamid=None,
            error="type=OAuthException; error_subcode=131026; message=x",
        )
    )
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["undeliverable"] == 0
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    view = views[f"order_created:{gid}"]
    assert view.state == "queued"  # retried, not terminally undeliverable
    assert view.attempts == 1


async def test_top_level_code_is_undeliverable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The mirror case: a real top-level `code=131026` IS terminal (undeliverable).
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/1"
    await _seed_row(gid)
    sender = FakeSender(
        SendResult(ok=False, status_code=400, wamid=None, error="code=131026; message=x")
    )
    _install_sender(monkeypatch, sender)

    result = await run_outbox_drain(c)

    assert result["undeliverable"] == 1
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_created:{gid}"].state == "undeliverable"


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
