import logging

from app.channels.whatsapp_inbound import InboundStatus
from app.deps import Container

logger = logging.getLogger("app.core.apply_status")


async def apply_delivery_status(c: Container, status: InboundStatus) -> None:
    """Apply one Meta delivery/read status update, trying outbound_messages first (template
    sends) then messages (AI replies) -- a wamid belongs to exactly one of the two tables, so
    the first match wins and the second lookup is skipped. A status for a wamid this app never
    sent (or sent before this feature existed) is a silent no-op, never an error.
    """
    found = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if not found:
        found = await c.conversations.apply_message_delivery_status(status.wamid, status.status)
    # A 'failed' status is WhatsApp's definitive "this did not go through" signal -- always warn,
    # regardless of which table matched (the store has a documented prior incident of a silent
    # delivery failure caught only by prolonged manual diagnosis). Wamids are not secrets/PII, so
    # they are safe to log verbatim.
    if status.status == "failed":
        logger.warning("whatsapp delivery failed for wamid=%s", status.wamid)
    elif not found:
        logger.debug(
            "delivery status update for unknown wamid=%s (status=%s)",
            status.wamid,
            status.status,
        )
