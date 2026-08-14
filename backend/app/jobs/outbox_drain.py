"""Outbox drain job (Flow A) — send the queued order-confirmation templates.

The 1-minute Vercel Cron calls this via ``GET /internal/jobs/outbox_drain`` (a manual/admin
``POST`` with ``X-Cron-Secret`` also works). It is the ONLY sender: the ``orders/create`` webhook
merely queues a row and acks Shopify — it never sends inline or via a background task (ADR-001;
Vercel's Python runtime has no reliable process-after-response). Each run atomically claims queued
``outbound_messages`` rows (``queued -> processing``) and sends each ``order_confirmation_cod``
template with the deterministic ``order:confirm:{gid}`` / ``order:cancel:{gid}`` quick-reply button
payloads.

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

from app.admin.controls import AdminControls, load_controls
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_sender import WhatsAppSendError, send_template
from app.core.send_policy import send_decision
from app.deps import Container
from app.store.base import OutboundClaim

logger = logging.getLogger("app.jobs.outbox_drain")

# Both an original push (order_created:{gid}) and its 1-hour reminder (order_reminder:{gid}, queued
# by jobs.reminders) carry the SAME order gid and send the SAME template with the SAME
# order:confirm/cancel:{gid} buttons — so the drain treats them identically; only the dedupe_key
# prefix differs (that distinct key is the reminder's exactly-once guarantee). The gid is recovered
# by stripping whichever prefix is present.
_DEDUPE_PREFIXES = ("order_created:", "order_reminder:")
# Rows claimed per cron tick. Kept small so a run comfortably fits inside any reasonable
# per-invocation time budget: send_template's per-call timeout is 20s, and Vercel's platform
# timeout on this legacy-`builds` config may be as low as the ~10-15s default (maxDuration unset).
# 5/min = 7200/day, well above the v1 target of 100-500 orders/day; a leftover row is simply
# picked up by the next 1-minute tick, and any row stranded 'processing' by a killed invocation is
# reclaimed by claim_queued_outbound's staleness predicate.
_CLAIM_LIMIT = 5

# Short, path-specific timeout for the INLINE order-confirmation send (send_inline_outbound),
# which runs synchronously inside the Shopify orders/create webhook request BEFORE the ack. It is
# deliberately far below send_template's 20s default (used by the cron drain, which has no ack
# constraint): the whole webhook handler — DB writes + this bounded send + overhead — must stay
# safely under Shopify's <5s ack requirement even in the worst case (a slow/unresponsive Meta
# endpoint). A send that does not finish in this window is bumped for the backstop drain to retry.
_INLINE_SEND_TIMEOUT_SECONDS = 3.0

# Meta send-error codes that mean "will never be delivered" (recipient not reachable / not on
# WhatsApp / re-engagement outside the window) — terminal, do NOT retry. These are the provider's
# error `code` field, surfaced by whatsapp_sender._safe_error as "code=<n>" in SendResult.error
# (the HTTP status for all of them is 400, so it cannot distinguish them; the Meta code can).
_UNDELIVERABLE_CODES = {131026, 131047, 131049}
# Anchored to the TOP-LEVEL `code=` field only: _safe_error renders it as the FIRST part or after
# a "; " separator, so requiring `^` or "; " before `code=` stops a substring match inside e.g.
# `error_subcode=131026` (a different field) being misread as the top-level send-error code.
_META_CODE_RE = re.compile(r"(?:^|; )code=(\d+)")


@dataclass(frozen=True)
class _TemplatePayload:
    template: str
    language: str
    customer_name: str
    order_name: str
    amount: str


def _gid_from_dedupe_key(dedupe_key: str) -> str | None:
    """'order_created:gid://shopify/Order/1' -> 'gid://shopify/Order/1'; else None.

    Accepts the reminder prefix too ('order_reminder:...'). The gid itself contains ':' so the
    prefix is stripped whole rather than split on ':'.
    """
    for prefix in _DEDUPE_PREFIXES:
        if dedupe_key.startswith(prefix):
            gid = dedupe_key[len(prefix):]
            return gid if gid.startswith("gid://") else None
    return None


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


# Outcome of processing one row, returned by send_one_outbound so each caller can count/branch
# without re-deriving the row's fate. RETRY covers both a transport error and a non-terminal Meta
# error (the row is bumped but stays queued for a later run_outbox_drain pass) — neither is a
# terminal count in the drain's summary, matching its pre-extraction behaviour.
OUTCOME_SENT = "sent"
OUTCOME_SUPPRESSED = "suppressed"
OUTCOME_UNDELIVERABLE = "undeliverable"
OUTCOME_FAILED = "failed"
OUTCOME_RETRY = "retry"


async def send_one_outbound(
    c: Container,
    cfg: WhatsAppConfig,
    controls: AdminControls,
    row: OutboundClaim,
    timeout: float | None = None,
) -> str:
    """Run the full per-row send state machine for ONE claimed outbound row; return its outcome.

    The row is already in state ``processing`` (``claim_queued_outbound`` /
    ``claim_outbound_by_id`` flipped it atomically) by the time this function sees it — this
    function never re-reads or assumes ``state == 'queued'``.
    On success it calls ``mark_outbound_sent`` (``processing -> sent``); on a Meta-undeliverable
    code ``mark_outbound_undeliverable`` (``processing -> undeliverable``); on a transport/retryable
    error ``bump_outbound_attempt``, which moves the row ``processing -> queued`` (or ``failed`` at
    the attempt cap) so a LATER cron run can re-claim it — it is never left stuck in ``processing``.

    ``timeout`` overrides the WhatsApp call's per-request timeout. When None (the cron-drain path)
    ``send_template``'s own generous default (20s) applies — a cron invocation has no <5s ack
    constraint. The inline order-confirmation send (``send_inline_outbound``) passes a SHORT
    timeout so the Shopify webhook it runs inside stays well under Shopify's <5s ack budget.

    Never raises ``WhatsAppSendError`` — a transport error is logged and the row bumped for a future
    retry. Nothing here mutates a Shopify order; the outbox only SENDS.
    """
    gid = _gid_from_dedupe_key(row.dedupe_key)
    if gid is None:
        # Corrupt dedupe_key -> terminal failure (max_attempts=1 flips it to 'failed'
        # immediately). Never reaches the sender, so the rest of the queue is unaffected.
        await c.ingest.bump_outbound_attempt(row.id, "bad_dedupe_key", max_attempts=1)
        return OUTCOME_FAILED

    decision = send_decision(controls.send_mode, controls.allowlist_phones, row.phone_e164)
    if decision == "suppress":
        await c.ingest.mark_outbound_suppressed(row.id)
        return OUTCOME_SUPPRESSED

    payload = _parse_payload(row.payload_json)
    if payload is None:
        await c.ingest.mark_outbound_undeliverable(row.id, "bad_payload")
        return OUTCOME_UNDELIVERABLE

    buttons = [f"order:confirm:{gid}", f"order:cancel:{gid}"]
    body_params = [payload.customer_name, payload.order_name, payload.amount]
    try:
        # Only pass `timeout` when the caller supplied one, so the default (cron) path keeps
        # send_template's own 20s default untouched — the inline path passes a short timeout.
        if timeout is None:
            result = await send_template(
                c.http, cfg, row.phone_e164, payload.template, payload.language,
                body_params, button_payloads=buttons,
            )
        else:
            result = await send_template(
                c.http, cfg, row.phone_e164, payload.template, payload.language,
                body_params, button_payloads=buttons, timeout=timeout,
            )
    except WhatsAppSendError:
        # Transport/timeout failure: retryable. Bump and move on — never abort the drain.
        # The exception text (an httpx network error) carries no secret, but is not logged
        # to keep the drain output clean; the row id is enough to trace.
        logger.warning("outbox drain: transport error sending row %s (retry queued)", row.id)
        await c.ingest.bump_outbound_attempt(row.id, "transport_error")
        return OUTCOME_RETRY

    if result.ok:
        await c.ingest.mark_outbound_sent(row.id, result.wamid)
        await c.ingest.set_mapping_status(gid, "template_sent")
        return OUTCOME_SENT

    code = _meta_error_code(result.error)
    if code in _UNDELIVERABLE_CODES:
        await c.ingest.mark_outbound_undeliverable(row.id, str(code))
        return OUTCOME_UNDELIVERABLE
    state = await c.ingest.bump_outbound_attempt(row.id, str(result.status_code))
    return OUTCOME_FAILED if state == "failed" else OUTCOME_RETRY


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
        outcome = await send_one_outbound(c, cfg, controls, row)
        if outcome == OUTCOME_SENT:
            sent += 1
        elif outcome == OUTCOME_SUPPRESSED:
            suppressed += 1
        elif outcome == OUTCOME_UNDELIVERABLE:
            undeliverable += 1
        elif outcome == OUTCOME_FAILED:
            failed += 1
        # OUTCOME_RETRY: bumped but still queued -> not a terminal count, exactly as before.

    return {
        "drained": len(rows),
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "undeliverable": undeliverable,
    }


async def send_inline_outbound(c: Container, outbound_id: int | None) -> str | None:
    """Send the JUST-QUEUED order-confirmation row inline, bounded by a short timeout.

    Owner-directed reversal (ADR-001 amendment, 2026-08-15): the external scheduler that drove
    the outbox drain stopped working, so the primary confirmation send now happens inline in the
    Shopify ``orders/create`` webhook request — a genuine ``await`` before the ack, NOT a
    ``BackgroundTask``/``create_task`` (which do not reliably run after the response on Vercel's
    Python runtime). Shopify's ack is therefore legitimately delayed by the send, so the WhatsApp
    call is bounded by ``_INLINE_SEND_TIMEOUT_SECONDS`` to keep the handler under the <5s budget.

    Concurrency safety: this operates ONLY on the single row ``ingest_order_created`` just wrote
    (``outbound_id``). It claims exactly that row by id (``queued -> processing``, the same atomic
    discipline as ``claim_queued_outbound``), so the still-available backstop
    ``/internal/jobs/outbox_drain`` can never also claim and double-send it. If the row is already
    claimed (a drain raced it) or not queued, ``claim_outbound_by_id`` returns None and nothing is
    sent. A retryable failure/timeout leaves the row re-claimable by that drain for a later retry.

    Mirrors ``run_outbox_drain``'s guards: ``send_mode == 'off'`` leaves the row queued and sends
    nothing; the ``send_mode`` kill switch is otherwise enforced per-row inside
    ``send_one_outbound`` (``send_decision`` -> shadow/allowlist-miss suppress with zero Meta
    calls). Returns the per-row outcome, or None when nothing was attempted (no fresh row /
    ``off`` / unconfigured / lost claim).

    NEVER raises: an inline-send failure must never block the webhook's 200 ack to Shopify.
    ``send_one_outbound`` already swallows transport + Meta errors; this outer guard additionally
    catches any unexpected error in loading controls/config or claiming the row.
    """
    if outbound_id is None:
        return None
    try:
        controls = await load_controls(c.config)
        if controls.send_mode == "off":
            # Leave the row queued for the backstop drain, exactly as run_outbox_drain does.
            return None
        cfg = await load_whatsapp_config(c.config)
        if cfg is None:
            return None
        claim = await c.ingest.claim_outbound_by_id(outbound_id)
        if claim is None:
            return None
        return await send_one_outbound(
            c, cfg, controls, claim, timeout=_INLINE_SEND_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("inline outbox send failed for row %s (ack unaffected)", outbound_id)
        return None
