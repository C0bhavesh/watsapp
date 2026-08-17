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
        await c.conversations.apply_message_delivery_status(status.wamid, status.status)
