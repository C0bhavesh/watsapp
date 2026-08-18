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

_ALERT_TEMPLATE = (
    "Thetavas bot: a message to {phone} failed to deliver after {max_retries} retries and "
    "was not sent. You may want to follow up another way."
)


async def _alert_owner_retry_exhausted(
    c: Container, wa_cfg: WhatsAppConfig, owner_number: str, phone: str
) -> None:
    """Tell the store owner a message could not be delivered after every retry was used.

    Mirrors ``core/conversation.py::_alert_owner``'s defensive shape (degrade silently if the alert
    number is unset, never raise, log-only on a failed alert send) but with its own message -- this
    is a different situation (a delivery permanently failed) from that function's handoff-alert
    wording, so it is a separate function rather than a reuse of that private helper.
    """
    if not owner_number:
        return
    alert = _ALERT_TEMPLATE.format(phone=phone, max_retries=MAX_RETRIES)
    try:
        result = await send_text(c.http, wa_cfg, owner_number, alert)
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
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    payload = parse_payload(info.payload_json)
    if payload is None:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, info.phone_e164)
    if decision == "suppress":
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    try:
        result = await send_template(
            c.http, wa_cfg, info.phone_e164, payload.template, payload.language,
            payload.body_params, button_payloads=payload.buttons,
            header_image_url=payload.image_url,
        )
    except WhatsAppSendError:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    if result.ok and result.wamid:
        await c.ingest.record_outbound_retry(info.id, result.wamid)
    else:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)


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
            c, wa_cfg, controls.owner_alert_number, phone or "unknown recipient"
        )
        return

    phone = await c.conversations.get_user_id(info.conversation_id)
    if phone is None:
        # Conversation row vanished (should not happen in practice) -- nothing sensible to resend,
        # so this attempt earns no new wamid and is terminal: page the owner like every other
        # dead-end branch in this module.
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(
            c, wa_cfg, controls.owner_alert_number, "unknown recipient"
        )
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, phone)
    if decision == "suppress":
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
        return

    try:
        result = await send_text(c.http, wa_cfg, phone, info.content)
    except WhatsAppSendError:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
        return

    if result.ok and result.wamid:
        await c.conversations.record_message_retry(info.id, result.wamid)
    else:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
