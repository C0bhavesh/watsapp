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
    # Each store method returns "not_found" / "applied" / "unchanged" (see app.store.base). Here we
    # only need the found-vs-not-found routing: try outbound_messages first, fall back to messages
    # only when the wamid was not in the first table. (Task 5 will consume the applied/unchanged
    # distinction to trigger a retry solely on a genuinely new 'failed'.)
    result = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if result == "not_found":
        result = await c.conversations.apply_message_delivery_status(status.wamid, status.status)
    # A 'failed' status is WhatsApp's definitive "this did not go through" signal -- always warn,
    # regardless of which table matched (the store has a documented prior incident of a silent
    # delivery failure caught only by prolonged manual diagnosis). Wamids are not secrets/PII, so
    # they are safe to log verbatim.
    if status.status == "failed":
        logger.warning("whatsapp delivery failed for wamid=%s", status.wamid)
    elif result == "not_found":
        logger.debug(
            "delivery status update for unknown wamid=%s (status=%s)",
            status.wamid,
            status.status,
        )
