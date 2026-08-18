"""Outbox drain job (Flow A) — send the queued order-confirmation templates.

Since the ADR-001 amendment (2026-08-15), the PRIMARY sender is ``send_inline_outbound`` (defined
below): the ``orders/create`` webhook queues a row and then sends THAT row inline — a bounded,
single-row ``await`` before the ack — because the external scheduler that used to drive the drain
stopped working. ``run_outbox_drain`` remains as a manual/backstop tool: it is invoked via
``GET /internal/jobs/outbox_drain`` (a manual/admin ``POST`` with ``X-Cron-Secret`` also works) and
picks up anything the inline path could not complete (a shadow/off row left queued, a timed-out or
retryable send bumped back to ``queued``, or a row stranded ``processing`` by a killed inline
invocation). It is currently WITHOUT a working scheduler, so treat it as a manual backstop, not an
automatic safety net. Each run atomically claims queued ``outbound_messages`` rows
(``queued -> processing``) and sends each ``cod_confirmation`` template with the deterministic
``order:confirm:{gid}`` / ``order:cancel:{gid}`` quick-reply button payloads — byte-for-byte the
same per-row state machine (``send_one_outbound``) the inline path uses.

Safety:
- The ``send_mode`` kill switch (ADR-002) is enforced here in ONE place. ``off`` returns before
  touching the queue at all; every other mode is decided per-row by ``send_decision``.
- A per-row transport error or bad row never aborts the whole drain — it is recorded and the loop
  continues, so one poison row cannot block the rest of the queue.
- Nothing here mutates a Shopify order; the drain only SENDS. Mutations happen only on a
  deterministic button tap (``core.order_actions``).
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from app.admin.controls import AdminControls, load_controls
from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.channels.whatsapp_sender import SendResult, WhatsAppSendError, send_template
from app.core.send_policy import send_decision
from app.deps import Container
from app.store.base import OutboundClaim

logger = logging.getLogger("app.jobs.outbox_drain")

# Both an original push (order_created:{gid}) and its 1-hour reminder (order_reminder:{gid}, queued
# by jobs.reminders) carry the SAME order gid and send the SAME template with the SAME
# order:confirm/cancel:{gid} buttons — so the drain treats them identically; only the dedupe_key
# prefix differs (that distinct key is the reminder's exactly-once guarantee). The gid is recovered
# by stripping whichever prefix is present.
# The order-confirmation family (original push + its 1-hour reminder) is the ONLY family whose
# successful send advances order_mappings.status -- see the dedupe-family gate in
# send_one_outbound. Fulfillment notifications (Task 3) share none of that state machine.
_ORDER_CONFIRMATION_DEDUPE_PREFIXES = ("order_created:", "order_reminder:")
_FULFILLMENT_DEDUPE_PREFIXES = ("fulfillment_shipped:", "fulfillment_delivered:")
_DEDUPE_PREFIXES = _ORDER_CONFIRMATION_DEDUPE_PREFIXES + _FULFILLMENT_DEDUPE_PREFIXES
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
# endpoint). Enforced as a REAL total-elapsed bound via asyncio.wait_for around the whole send (a
# bare-float httpx timeout only caps each connect/read/write op, not the total, so a slow-but-not-
# hung endpoint could still exceed the budget). A send that does not finish in this window is cut
# off; its row is left recoverable by the backstop drain (bumped to queued on a retryable failure,
# or reclaimed from stale 'processing' by claim_queued_outbound's staleness predicate on timeout).
_INLINE_SEND_TIMEOUT_SECONDS = 3.0

# Meta send-error codes that mean "will never be delivered" (recipient not reachable / not on
# WhatsApp / re-engagement outside the window) — terminal, do NOT retry. These are the provider's
# error `code` field, surfaced by whatsapp_sender._safe_error as "code=<n>" in SendResult.error
# (the HTTP status for all of them is 400, so it cannot distinguish them; the Meta code can).
_UNDELIVERABLE_CODES = {131026, 131047, 131049}
# Meta media-fetch failures for a template's header IMAGE URL (131052 = unsupported/unfetchable
# media, 131053 = media download failed) — e.g. the merchant deleted/replaced the product image
# between order-time and a >=1h-later reminder replay (reminders.py replays payload_json verbatim).
# These fail the WHOLE send even though the MESSAGE body itself is fine — so they are NOT terminal:
# we retry the SAME row immediately WITHOUT the image (image-less send) rather than burn a normal
# attempt, honouring Q19a ("a Shopify/image hiccup must never block the confirmation message").
# Detected via the same top-level `code=` path _UNDELIVERABLE_CODES uses (_meta_error_code).
_MEDIA_ERROR_CODES = {131052, 131053}
# Anchored to the TOP-LEVEL `code=` field only: _safe_error renders it as the FIRST part or after
# a "; " separator, so requiring `^` or "; " before `code=` stops a substring match inside e.g.
# `error_subcode=131026` (a different field) being misread as the top-level send-error code.
_META_CODE_RE = re.compile(r"(?:^|; )code=(\d+)")


# Every payload_json must carry these three top-level keys; body_params is a NAMED dict (Meta
# named-placeholder templates, e.g. cod_confirmation) or a POSITIONAL list (Meta {{n}}-placeholder
# templates, e.g. order_shipped/order_delivered) -- mirrors send_template's own Mapping | Sequence
# duality. image_url and buttons are both optional (absence means "no header" / "no buttons").
_REQUIRED_PAYLOAD_KEYS = ("template", "language")


@dataclass(frozen=True)
class TemplatePayload:
    template: str
    language: str
    body_params: dict[str, str] | list[str]
    image_url: str | None = None
    buttons: list[str] = field(default_factory=list)


def _gid_from_dedupe_key(dedupe_key: str) -> str | None:
    """Strip a known dedupe_key prefix and return the trailing id; else None.

    The trailing id's MEANING differs by prefix family: for 'order_created:'/'order_reminder:' it
    is the ORDER gid (used below to advance order_mappings.status on a successful send); for
    'fulfillment_shipped:'/'fulfillment_delivered:' it is the FULFILLMENT gid (used only as an
    opaque validity check here -- the fulfillment-notification success path has no mapping-status
    side effect at all, see the dedupe-family gate near the end of send_one_outbound). Any
    prefix in _DEDUPE_PREFIXES is accepted; an unrecognized prefix (corrupt/garbage dedupe_key)
    returns None, which send_one_outbound treats as a terminal bad_dedupe_key failure.
    """
    for prefix in _DEDUPE_PREFIXES:
        if dedupe_key.startswith(prefix):
            gid = dedupe_key[len(prefix):]
            return gid if gid.startswith("gid://") else None
    return None


def parse_payload(payload_json: str) -> TemplatePayload | None:
    """Parse the generic outbox payload envelope; None on any bad/missing/malformed field.

    None -> the row is marked undeliverable (it can never render), which also terminally drops any
    legacy row still queued under an OLDER flat-key shape (no top-level ``body_params``) -- an
    already-retired template shape, so retrying it forever would be pointless.
    ``image_url`` is optional: only a public https link is kept (Meta rejects anything else),
    otherwise the header is simply skipped (Q19a graceful degradation). ``buttons`` is optional:
    absence or a non-list value means no button components (e.g. prepaid_order, order_shipped,
    order_delivered all have no Confirm/Cancel component on the WABA).
    """
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fields: dict[str, str] = {}
    for key in _REQUIRED_PAYLOAD_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
        fields[key] = value
    raw_body_params = data.get("body_params")
    body_params: dict[str, str] | list[str]
    if isinstance(raw_body_params, dict):
        if not raw_body_params or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw_body_params.items()
        ):
            return None
        body_params = raw_body_params
    elif isinstance(raw_body_params, list):
        if not raw_body_params or not all(isinstance(v, str) for v in raw_body_params):
            return None
        body_params = raw_body_params
    else:
        return None
    image = data.get("image_url")
    image_url = image if isinstance(image, str) and image.startswith("https://") else None
    raw_buttons = data.get("buttons")
    buttons = (
        raw_buttons
        if isinstance(raw_buttons, list) and all(isinstance(b, str) for b in raw_buttons)
        else []
    )
    return TemplatePayload(
        template=fields["template"],
        language=fields["language"],
        body_params=body_params,
        image_url=image_url,
        buttons=buttons,
    )


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

    payload = parse_payload(row.payload_json)
    if payload is None:
        # Observability (not new behaviour): a None parse is either a corrupt payload or a legacy
        # pre-deploy row (old order_confirmation_cod shape) that can no longer render on the WABA.
        # Both are terminally undeliverable by design — the old template is gone — but log the row
        # id (no payload contents: they may carry customer PII) so a post-deploy undeliverable
        # spike is grep-able in Vercel logs.
        logger.warning(
            "outbox drain: row %s marked undeliverable (bad/legacy payload shape)", row.id
        )
        await c.ingest.mark_outbound_undeliverable(row.id, "bad_payload")
        return OUTCOME_UNDELIVERABLE

    # buttons/body_params now come straight from the payload envelope (Task 1's generalization) --
    # no per-template string check here anymore. Any template with no Confirm/Cancel component on
    # the WABA (prepaid_order, order_shipped, order_delivered) simply carries an empty/absent
    # "buttons" list, which parse_payload already normalized to [].
    buttons = payload.buttons
    body_params = payload.body_params
    async def _send(header_image_url: str | None) -> SendResult:
        # Only pass `timeout` when the caller supplied one, so the default (cron) path keeps
        # send_template's own 20s default untouched — the inline path passes a short timeout.
        if timeout is None:
            return await send_template(
                c.http, cfg, row.phone_e164, payload.template, payload.language,
                body_params, button_payloads=buttons, header_image_url=header_image_url,
            )
        return await send_template(
            c.http, cfg, row.phone_e164, payload.template, payload.language,
            body_params, button_payloads=buttons, header_image_url=header_image_url,
            timeout=timeout,
        )

    try:
        result = await _send(payload.image_url)
        # A stale/unfetchable header image (the merchant changed the product photo between
        # order-time and a later reminder replay) makes Meta reject the ENTIRE send with a media
        # code — but the MESSAGE itself is fine, only the header can't be fetched. Retry THIS row
        # once, immediately, WITHOUT the image so the customer still gets the confirmation, rather
        # than counting it as a failed attempt and eventually landing the row `failed` with no
        # message sent (Q19a). Guarded on `payload.image_url` so the image-less retry runs at most
        # once. The retry's result then flows through the SAME classification below.
        if (
            not result.ok
            and payload.image_url is not None
            and _meta_error_code(result.error) in _MEDIA_ERROR_CODES
        ):
            logger.warning(
                "outbox drain: row %s header image unfetchable (Meta media error); "
                "retrying image-less", row.id,
            )
            result = await _send(None)
    except WhatsAppSendError:
        # Transport/timeout failure: retryable. Bump and move on — never abort the drain.
        # The exception text (an httpx network error) carries no secret, but is not logged
        # to keep the drain output clean; the row id is enough to trace.
        logger.warning("outbox drain: transport error sending row %s (retry queued)", row.id)
        await c.ingest.bump_outbound_attempt(row.id, "transport_error")
        return OUTCOME_RETRY

    if result.ok:
        await c.ingest.mark_outbound_sent(row.id, result.wamid)
        # Only the order-confirmation family (original push + its 1-hour reminder) has anything to
        # do with order_mappings' confirm/cancel state machine. A fulfillment notification's `gid`
        # here is a FULFILLMENT gid, not an order gid -- writing it into order_mappings would be
        # wrong even if it happened to not error, so this is gated on the dedupe_key's own prefix
        # family, not on `gid`'s mere presence.
        if row.dedupe_key.startswith(_ORDER_CONFIRMATION_DEDUPE_PREFIXES):
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


async def send_inline_outbound(
    c: Container, outbound_id: int | None, timeout: float = _INLINE_SEND_TIMEOUT_SECONDS,
) -> str | None:
    """Send the JUST-QUEUED row inline, bounded by ``timeout`` (defaults to
    ``_INLINE_SEND_TIMEOUT_SECONDS``, the order-confirmation call site's existing behavior,
    unchanged for any caller that doesn't pass an override).

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
        # asyncio.wait_for gives a REAL total-elapsed bound, independent of how httpx internally
        # splits connect/read/write timeouts — a slow-but-not-hung Meta endpoint can never hold
        # the ack past the budget. A timeout raises TimeoutError, caught below like any other
        # failure (row stays recoverable for the backstop drain; never propagates past the ack).
        return await asyncio.wait_for(
            send_one_outbound(c, cfg, controls, claim, timeout=timeout),
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(
            "inline outbox send failed for row %s: %s (ack unaffected)",
            outbound_id, type(exc).__name__,
        )
        return None
