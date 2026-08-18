import logging

from app.admin.controls import load_controls
from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_inbound import InboundStatus
from app.core.delivery_retry import retry_failed_message, retry_failed_outbound
from app.deps import Container

logger = logging.getLogger("app.core.apply_status")


async def apply_delivery_status(
    c: Container, wa_cfg: WhatsAppConfig, status: InboundStatus
) -> None:
    """Apply one Meta delivery/read status update, trying outbound_messages first (template
    sends) then messages (AI replies) -- a wamid belongs to exactly one of the two tables, so
    the first match wins and the second lookup is skipped. A status for a wamid this app never
    sent (or sent before this feature existed) is a silent no-op, never an error.

    On a 'failed' status that GENUINELY newly applied (result "applied", not a Meta-redelivered
    duplicate that maps to "unchanged"), the corresponding table's resend is triggered exactly
    once -- never on "unchanged"/"not_found" or any other status. AdminControls (send mode +
    allowlist + owner alert number) is loaded HERE rather than passed in, so channels/whatsapp.py
    stays admin-free (fastapi-layering); core/ is already free to depend on app.admin.controls.
    """
    # Each store method returns "not_found" / "applied" / "unchanged" (see app.store.base). Route
    # outbound_messages first; only fall back to messages when the wamid was not in the first table.
    # The applied/unchanged distinction is what gates the retry below: "applied" is a fresh failure,
    # "unchanged" is a duplicate/regressive redelivery that must NOT retry-storm.
    outbound_result = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if outbound_result == "not_found":
        message_result = await c.conversations.apply_message_delivery_status(
            status.wamid, status.status
        )
    else:
        message_result = None
    # A 'failed' status is WhatsApp's definitive "this did not go through" signal -- always warn,
    # regardless of which table matched (the store has a documented prior incident of a silent
    # delivery failure caught only by prolonged manual diagnosis). Wamids are not secrets/PII, so
    # they are safe to log verbatim. Only a genuinely NEW failure ("applied") drives a resend.
    if status.status == "failed":
        logger.warning("whatsapp delivery failed for wamid=%s", status.wamid)
        # Both retry branches need the same AdminControls (send mode + allowlist + owner alert
        # number), so load it once here rather than duplicating the call inside each branch.
        if outbound_result == "applied" or message_result == "applied":
            controls = await load_controls(c.config)
            if outbound_result == "applied":
                await retry_failed_outbound(c, wa_cfg, controls, status.wamid)
            else:
                await retry_failed_message(c, wa_cfg, controls, status.wamid)
    elif outbound_result == "not_found" and message_result == "not_found":
        logger.debug(
            "delivery status update for unknown wamid=%s (status=%s)",
            status.wamid,
            status.status,
        )
