"""Outbox drain job (Flow A) — send the queued order-confirmation templates.

A cron/self-invoke calls this via ``GET|POST /internal/jobs/outbox_drain``. It claims queued
``outbound_messages`` rows and sends each ``order_confirmation_cod`` template with the deterministic
``order:confirm:{gid}`` / ``order:cancel:{gid}`` quick-reply button payloads.

Safety:
- The ``send_mode`` kill switch (ADR-002) is enforced here in ONE place. ``off`` returns before
  touching the queue at all; every other mode is decided per-row by ``send_decision``.
- A per-row transport error or bad row never aborts the whole drain — it is recorded and the loop
  continues, so one poison row cannot block the rest of the queue.
- Nothing here mutates a Shopify order; the drain only SENDS. Mutations happen only on a
  deterministic button tap (``core.order_actions``).
"""

import json
import logging
import re
from dataclasses import dataclass

from app.admin.controls import load_controls
from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_sender import WhatsAppSendError, send_template
from app.core.send_policy import send_decision
from app.deps import Container

logger = logging.getLogger("app.jobs.outbox_drain")

_DEDUPE_PREFIX = "order_created:"
_CLAIM_LIMIT = 25

# Meta send-error codes that mean "will never be delivered" (recipient not reachable / not on
# WhatsApp / re-engagement outside the window) — terminal, do NOT retry. These are the provider's
# error `code` field, surfaced by whatsapp_sender._safe_error as "code=<n>" in SendResult.error
# (the HTTP status for all of them is 400, so it cannot distinguish them; the Meta code can).
_UNDELIVERABLE_CODES = {131026, 131047, 131049}
_META_CODE_RE = re.compile(r"code=(\d+)")


@dataclass(frozen=True)
class _TemplatePayload:
    template: str
    language: str
    customer_name: str
    order_name: str
    amount: str


def _gid_from_dedupe_key(dedupe_key: str) -> str | None:
    """'order_created:gid://shopify/Order/1' -> 'gid://shopify/Order/1'; else None.

    The gid itself contains ':' so the prefix is stripped whole rather than split on ':'.
    """
    if not dedupe_key.startswith(_DEDUPE_PREFIX):
        return None
    gid = dedupe_key[len(_DEDUPE_PREFIX):]
    return gid if gid.startswith("gid://") else None


def _parse_payload(payload_json: str) -> _TemplatePayload | None:
    """Parse the Phase 2 outbox payload; None on any bad/missing field (-> undeliverable)."""
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = {}
    for key in ("template", "language", "customer_name", "order_name", "amount"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
        fields[key] = value
    return _TemplatePayload(**fields)


def _meta_error_code(error: str | None) -> int | None:
    if not error:
        return None
    match = _META_CODE_RE.search(error)
    return int(match.group(1)) if match else None


async def run_outbox_drain(c: Container) -> dict[str, object]:
    controls = await load_controls(c.config)
    if controls.send_mode == "off":
        return {"drained": 0, "sent": 0, "suppressed": 0, "reason": "send_mode off"}
    cfg = await load_whatsapp_config(c.config)
    if cfg is None:
        return {"drained": 0, "error": "whatsapp not configured"}

    rows = await c.ingest.claim_queued_outbound(limit=_CLAIM_LIMIT)
    sent = suppressed = failed = undeliverable = 0
    for row in rows:
        gid = _gid_from_dedupe_key(row.dedupe_key)
        if gid is None:
            # Corrupt dedupe_key -> terminal failure (max_attempts=1 flips it to 'failed'
            # immediately). Never reaches the sender, so the rest of the queue is unaffected.
            await c.ingest.bump_outbound_attempt(row.id, "bad_dedupe_key", max_attempts=1)
            failed += 1
            continue

        decision = send_decision(controls.send_mode, controls.allowlist_phones, row.phone_e164)
        if decision == "suppress":
            await c.ingest.mark_outbound_suppressed(row.id)
            suppressed += 1
            continue

        payload = _parse_payload(row.payload_json)
        if payload is None:
            await c.ingest.mark_outbound_undeliverable(row.id, "bad_payload")
            undeliverable += 1
            continue

        try:
            result = await send_template(
                c.http, cfg, row.phone_e164, payload.template, payload.language,
                [payload.customer_name, payload.order_name, payload.amount],
                button_payloads=[f"order:confirm:{gid}", f"order:cancel:{gid}"],
            )
        except WhatsAppSendError:
            # Transport/timeout failure: retryable. Bump and move on — never abort the drain.
            # The exception text (an httpx network error) carries no secret, but is not logged
            # to keep the drain output clean; the row id is enough to trace.
            logger.warning("outbox drain: transport error sending row %s (retry queued)", row.id)
            await c.ingest.bump_outbound_attempt(row.id, "transport_error")
            continue

        if result.ok:
            await c.ingest.mark_outbound_sent(row.id, result.wamid)
            await c.ingest.set_mapping_status(gid, "template_sent")
            sent += 1
            continue

        code = _meta_error_code(result.error)
        if code in _UNDELIVERABLE_CODES:
            await c.ingest.mark_outbound_undeliverable(row.id, str(code))
            undeliverable += 1
        else:
            state = await c.ingest.bump_outbound_attempt(row.id, str(result.status_code))
            if state == "failed":
                failed += 1

    return {
        "drained": len(rows),
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "undeliverable": undeliverable,
    }
