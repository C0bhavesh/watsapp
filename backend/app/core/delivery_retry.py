"""Delivery-failure auto-retry: resend a message that Meta reported as 'failed', page the owner
once every retry is exhausted (or a resend can never earn a future wamid to retry off of).

Two sibling entry points -- one per table -- share the same shape:
- ``retry_failed_outbound`` for a template send (``outbound_messages``);
- ``retry_failed_message`` for an AI reply (``messages``).

Each is called only when the apply-status orchestrator (Task 1) confirms a wamid's 'failed' was a
GENUINELY NEW transition, never a Meta-redelivered duplicate -- so a single fresh failure drives at
most one resend here. A resend IS a real outbound send: it goes through the exact same
``send_decision(send_mode, allowlist, phone)`` kill switch (ADR-002) as every other send in this
codebase -- no bypass. The retry count is capped at ``MAX_RETRIES``; a suppressed/failed/
unrenderable resend earns no new wamid to hang the next retry off, so it is treated as terminal for
this row and pages the owner immediately rather than waiting to reach the cap.

Nothing here mutates a Shopify order -- it only SENDS (CLAUDE.md Rule 2).
"""

import logging

from app.admin.controls import AdminControls
from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_sender import WhatsAppSendError, send_template, send_text
from app.core.send_policy import send_decision
from app.deps import Container
from app.jobs.outbox_drain import parse_payload

logger = logging.getLogger("app.core.delivery_retry")

MAX_RETRIES = 3

# Fallback last_error_code for a row that exhausts its retries without any Meta error code ever
# having been captured (a 'failed' status that carried no errors[] array). Records WHY the row is
# terminal so the admin outbox view never shows a bare NULL reason on an undeliverable send.
_RETRIES_EXHAUSTED_CODE = "retries_exhausted"

# Short, path-specific timeout for every send this module makes -- both the customer resend and the
# owner alert. Both run inline inside the WhatsApp webhook request (via apply_status), which shares
# one TURN_TIMEOUT_SECONDS budget with any inbound customer messages in the same payload; a single
# failed-status webhook can trigger a resend PLUS an owner alert, so at send_text/send_template's
# 20s default the pair could burn up to 40s and silently drop a customer's message on an already
# 200-acked webhook. Bounded to 3.0s (matching outbox_drain._INLINE_SEND_TIMEOUT_SECONDS): a
# timeout just raises WhatsAppSendError sooner, which every call site already handles (burn the
# retry, page the owner); Meta's redelivery of an un-acked request maps to "unchanged" (no double
# retry), so a short cut-off costs nothing.
_RETRY_SEND_TIMEOUT_SECONDS = 3.0

_ALERT_TEMPLATE = (
    "Thetavas bot: a message to {phone} failed to deliver after {retry_count} retry attempt(s) "
    "and was not sent. You may want to follow up another way."
)


async def _alert_owner_retry_exhausted(
    c: Container,
    wa_cfg: WhatsAppConfig,
    controls: AdminControls,
    phone: str,
    retry_count: int,
) -> None:
    """Tell the store owner a message could not be delivered after every retry was used.

    Mirrors ``core/conversation.py::_alert_owner``'s defensive shape (degrade silently if the alert
    number is unset, never raise, log-only on a failed alert send) but with its own message -- this
    is a different situation (a delivery permanently failed) from that function's handoff-alert
    wording, so it is a separate function rather than a reuse of that private helper.

    The alert is itself a real outbound WhatsApp send, so it goes through the SAME
    ``send_decision`` kill switch as every other send in this module (ADR-002) -- flipping
    ``send_mode`` to ``off`` during an incident must stop this path too, not just the resend. A
    suppressed alert simply does not send (debug-log, no warning -- an expected kill-switch state,
    not a failure). ``retry_count`` is the number of retry attempts actually SPENT on this row, so
    the wording is accurate even when it fires from an early-terminal branch (0 or 1 attempts).
    """
    owner_number = controls.owner_alert_number
    if not owner_number:
        return
    if send_decision(controls.send_mode, controls.allowlist_phones, owner_number) == "suppress":
        logger.info(
            "retry-exhausted owner alert suppressed by kill switch (send_mode=%s)",
            controls.send_mode,
        )
        return
    alert = _ALERT_TEMPLATE.format(phone=phone, retry_count=retry_count)
    try:
        result = await send_text(
            c.http, wa_cfg, owner_number, alert, timeout=_RETRY_SEND_TIMEOUT_SECONDS
        )
    except WhatsAppSendError:
        logger.warning("retry-exhausted owner alert failed to send (transport error)")
        return
    if not result.ok:
        logger.warning(
            "retry-exhausted owner alert failed: status=%s error=%s",
            result.status_code, result.error,
        )


async def retry_failed_outbound(
    c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str
) -> None:
    """Resend a template that just had a genuinely new 'failed' delivery status applied.

    No-ops if the wamid is unknown to ``outbound_messages`` (the caller tries the messages table in
    that case). At/over ``MAX_RETRIES`` it stops resending and pages the owner. A suppressed (kill
    switch), unrenderable (bad payload), or unsendable (transport error / non-ok response) attempt
    burns one retry, earns no fresh wamid, and pages the owner immediately.
    """
    info = await c.ingest.get_outbound_retry_info(wamid)
    if info is None:
        return
    if info.retry_count >= MAX_RETRIES:
        # Retries exhausted: transition the row OUT of 'sent' to 'undeliverable' so the admin outbox
        # view stops rendering it as a healthy send. Stamp the Meta error code captured from the
        # failed status (info.last_error_code), falling back to a constant when none was recorded.
        await c.ingest.mark_outbound_undeliverable(
            info.id, info.last_error_code or _RETRIES_EXHAUSTED_CODE
        )
        await _alert_owner_retry_exhausted(
            c, wa_cfg, controls, info.phone_e164, info.retry_count
        )
        return

    # Every terminal branch below burns one retry, so the count actually SPENT once this attempt
    # ends is info.retry_count + 1 -- that is what the owner alert reports.
    spent = info.retry_count + 1

    payload = parse_payload(info.payload_json)
    if payload is None:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, info.phone_e164, spent)
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, info.phone_e164)
    if decision == "suppress":
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, info.phone_e164, spent)
        return

    try:
        result = await send_template(
            c.http, wa_cfg, info.phone_e164, payload.template, payload.language,
            payload.body_params, button_payloads=payload.buttons,
            header_image_url=payload.image_url, timeout=_RETRY_SEND_TIMEOUT_SECONDS,
        )
    except WhatsAppSendError:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, info.phone_e164, spent)
        return

    if result.ok and result.wamid:
        await c.ingest.record_outbound_retry(info.id, result.wamid)
        # Non-PII audit trail: wamids are established elsewhere as safe to log verbatim; the phone
        # and message content are NOT logged.
        logger.info(
            "delivery retry resent (table=outbound_messages old_wamid=%s new_wamid=%s "
            "retry_count=%s)",
            wamid, result.wamid, spent,
        )
    else:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, info.phone_e164, spent)


async def retry_failed_message(
    c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str
) -> None:
    """Resend an AI reply that just had a genuinely new 'failed' delivery status applied.

    Mirrors ``retry_failed_outbound`` exactly, for the ``messages``/AI-reply table -- see that
    function's docstring for the shared reasoning. The recipient is resolved from the conversation's
    ``user_id`` (the normalized phone).
    """
    info = await c.conversations.get_message_retry_info(wamid)
    if info is None:
        return
    if info.retry_count >= MAX_RETRIES:
        phone = await c.conversations.get_user_id(info.conversation_id)
        await _alert_owner_retry_exhausted(
            c, wa_cfg, controls, phone or "unknown recipient", info.retry_count
        )
        return

    # Every terminal branch below burns one retry, so the count actually SPENT once this attempt
    # ends is info.retry_count + 1 -- that is what the owner alert reports.
    spent = info.retry_count + 1

    phone = await c.conversations.get_user_id(info.conversation_id)
    if phone is None:
        # Conversation row vanished (should not happen in practice) -- nothing sensible to resend,
        # so this attempt earns no new wamid and is terminal: page the owner like every other
        # dead-end branch in this module.
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, "unknown recipient", spent)
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, phone)
    if decision == "suppress":
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, phone, spent)
        return

    try:
        result = await send_text(
            c.http, wa_cfg, phone, info.content, timeout=_RETRY_SEND_TIMEOUT_SECONDS
        )
    except WhatsAppSendError:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, phone, spent)
        return

    if result.ok and result.wamid:
        await c.conversations.record_message_retry(info.id, result.wamid)
        # Non-PII audit trail: wamids are safe to log verbatim; phone/content are NOT logged.
        logger.info(
            "delivery retry resent (table=messages old_wamid=%s new_wamid=%s retry_count=%s)",
            wamid, result.wamid, spent,
        )
    else:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls, phone, spent)
