"""Q17 reminder sweep job: queue ONE reminder (same template payload) for an order still at
status 'template_sent' 1 hour after the original push, exactly once, via the existing outbox."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.admin.controls import AdminControls, save_controls
from app.channels.whatsapp_sender import SendResult
from app.deps import get_container, reset_container
from app.jobs.outbox_drain import run_outbox_drain
from app.jobs.reminders import run_send_reminders
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919664290413"


def _payload(gid: str) -> dict[str, object]:
    # The generalized outbox envelope (Task 1): NAMED body_params for cod_confirmation, with the
    # Confirm/Cancel buttons baked in at ingest keyed to THIS order's gid (mirrors what
    # shopify_webhook now writes). The reminder replays payload_json verbatim, so its buttons carry
    # the same gid -- exactly as production does.
    return {
        "template": "cod_confirmation", "language": "en",
        "body_params": {
            "customer_name": "Suman", "order_id": "tavas3733",
            "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
            "product_amount": "949",
        },
        "image_url": "https://cdn.shopify.com/s/files/1/x.jpg",
        "buttons": [f"order:confirm:{gid}", f"order:cancel:{gid}"],
    }


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


async def _seed_template_sent(gid: str, age_minutes: int, status: str = "template_sent") -> None:
    c = get_container()
    mapping = MappingUpsert(
        order_gid=gid, order_name="tavas3733", order_number_int=3733, phone_e164=PHONE,
        customer_name="Suman", email=None, language="hi",
        financial_status_at_create="PENDING", is_cod=True,
    )
    draft = OutboundDraft(
        dedupe_key=f"order_created:{gid}", kind="order_confirmation",
        phone_e164=PHONE, payload_json=json.dumps(_payload(gid)),
    )
    await c.ingest.ingest_order_created(f"wh-{gid}", "orders/create", mapping, draft)
    await c.ingest.set_mapping_status(gid, status)
    # In-memory analogue of backdating order_mappings.updated_at -- the moment the mapping became
    # template_sent, which is what the sweep ages off (NOT created_at / webhook-ingest time).
    c.ingest._mapping_updated_at[gid] = datetime.now(UTC) - timedelta(minutes=age_minutes)


class FakeSender:
    def __init__(self, result: SendResult) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result

    async def __call__(
        self, http, cfg, to, template_name, language, body_params,
        button_payloads=(), header_image_url=None, timeout=20.0,
    ) -> SendResult:
        self.calls.append(
            {"to": to, "template": template_name, "language": language,
             "body_params": dict(body_params), "button_payloads": list(button_payloads),
             "header_image_url": header_image_url}
        )
        return self._result


async def test_stale_order_gets_one_reminder_queued_with_original_payload() -> None:
    c = get_container()
    gid = "gid://shopify/Order/1"
    await _seed_template_sent(gid, age_minutes=90)

    result = await run_send_reminders(c)

    assert result == {"swept": 1, "queued": 1}
    reminder = await c.ingest.find_outbound_by_dedupe_key(f"order_reminder:{gid}")
    assert reminder is not None
    assert reminder.phone_e164 == PHONE
    assert json.loads(reminder.payload_json) == _payload(gid)  # verbatim reuse of the push
    assert reminder.kind == "order_confirmation"


async def test_recent_order_not_reminded() -> None:
    c = get_container()
    gid = "gid://shopify/Order/2"
    await _seed_template_sent(gid, age_minutes=30)  # inside the 1-hour window

    result = await run_send_reminders(c)

    assert result == {"swept": 0, "queued": 0}
    assert await c.ingest.find_outbound_by_dedupe_key(f"order_reminder:{gid}") is None


async def test_freshly_sent_backlog_order_not_reminded() -> None:
    # HIGH #1: an order ingested long ago whose template only just SENT (updated_at = now) must
    # NOT be reminded on the next sweep -- the clock is anchored to the template-sent moment.
    c = get_container()
    gid = "gid://shopify/Order/backlog"
    await _seed_template_sent(gid, age_minutes=90)  # updated_at 90m ago
    # Simulate a backlog flush: the drain just re-stamped template_sent -> updated_at = now.
    await c.ingest.set_mapping_status(gid, "template_sent")

    result = await run_send_reminders(c)

    assert result == {"swept": 0, "queued": 0}
    assert await c.ingest.find_outbound_by_dedupe_key(f"order_reminder:{gid}") is None


async def test_very_old_order_excluded_by_ceiling() -> None:
    # HIGH #2: a 30-hour-old unanswered template_sent order is past the 24h ceiling -> never swept.
    c = get_container()
    gid = "gid://shopify/Order/ancient"
    await _seed_template_sent(gid, age_minutes=30 * 60)

    result = await run_send_reminders(c)

    assert result == {"swept": 0, "queued": 0}
    assert await c.ingest.find_outbound_by_dedupe_key(f"order_reminder:{gid}") is None


@pytest.mark.parametrize("status", ["confirmed", "cancel_requested"])
async def test_tapped_order_never_reminded(status: str) -> None:
    c = get_container()
    gid = "gid://shopify/Order/3"
    await _seed_template_sent(gid, age_minutes=120, status=status)

    result = await run_send_reminders(c)

    assert result == {"swept": 0, "queued": 0}
    assert await c.ingest.find_outbound_by_dedupe_key(f"order_reminder:{gid}") is None


async def test_sweep_twice_queues_exactly_one_reminder_row() -> None:
    c = get_container()
    gid = "gid://shopify/Order/4"
    await _seed_template_sent(gid, age_minutes=90)

    first = await run_send_reminders(c)
    second = await run_send_reminders(c)

    assert first == {"swept": 1, "queued": 1}
    # Status is STILL template_sent (no tap), so the order is swept again -> but the UNIQUE
    # dedupe_key makes the second enqueue a no-op: swept 1, queued 0.
    assert second == {"swept": 1, "queued": 0}
    rows = [
        v for v in await c.ingest.recent_outbound(20)
        if v.dedupe_key == f"order_reminder:{gid}"
    ]
    assert len(rows) == 1


async def test_queued_reminder_flows_through_existing_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: the reminder row the sweep queued is picked up and sent by the EXISTING
    # claim_queued_outbound -> send_one_outbound pipeline with no special-casing.
    c = get_container()
    await save_controls(c.config, AdminControls(send_mode="live"))
    gid = "gid://shopify/Order/5"
    await _seed_template_sent(gid, age_minutes=0)
    # Drain the ORIGINAL push first so only the reminder is left queued.
    sender = FakeSender(SendResult(ok=True, status_code=200, wamid="wamid.orig", error=None))
    monkeypatch.setattr("app.jobs.outbox_drain.send_template", sender)
    await run_outbox_drain(c)
    assert not await c.ingest.claim_queued_outbound()
    # The drain just re-stamped template_sent (updated_at = now, as in production); age it past the
    # 1-hour threshold so the sweep now considers it stale.
    c.ingest._mapping_updated_at[gid] = datetime.now(UTC) - timedelta(minutes=90)

    await run_send_reminders(c)

    drain = await run_outbox_drain(c)

    assert drain["sent"] == 1
    # The reminder was sent with the deterministic Confirm/Cancel buttons, same as any push.
    last = sender.calls[-1]
    assert last["to"] == PHONE
    assert last["template"] == "cod_confirmation"
    assert last["body_params"] == {
        "customer_name": "Suman", "order_id": "tavas3733",
        "product_name": "Blue Kurti", "product_color": "Blue", "product_size": "M",
        "product_amount": "949",
    }
    assert last["header_image_url"] == "https://cdn.shopify.com/s/files/1/x.jpg"
    assert last["button_payloads"] == [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    views = {v.dedupe_key: v for v in await c.ingest.recent_outbound(10)}
    assert views[f"order_reminder:{gid}"].state == "sent"


def test_send_reminders_registered_in_jobs() -> None:
    from app.jobs.router import JOBS

    assert JOBS["send_reminders"] is run_send_reminders
