import logging

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_inbound import InboundImage, InboundText
from app.channels.whatsapp_media import FetchedMedia, fetch_media
from app.core.phone import normalize_phone
from app.deps import Container, active_llm
from app.providers.base import ProviderError

logger = logging.getLogger("app.channels.whatsapp_image_intake")


async def handle_inbound_image(
    c: Container, cfg: WhatsAppConfig, event: InboundImage
) -> InboundText:
    """Turn an inbound image into a synthesized InboundText for run_turn.

    Downloads + stores the image and asks the active LLM to describe it (vision), then combines
    that description with the customer's caption (if any) into one plain-text message. Never
    raises: any failure (download, storage, or vision) degrades to using the caption alone, or a
    fixed placeholder if there is no caption either, so an inbound image always produces SOME
    turn instead of silently vanishing.
    """
    phone = normalize_phone(event.wa_id) or event.wa_id
    description: str | None = None

    media: FetchedMedia | None = await fetch_media(c.http, cfg, event.media_id)
    if media is not None:
        try:
            await c.ingest.save_inbound_image(
                phone, event.message_id, media.mime_type, media.bytes
            )
        except Exception:
            logger.exception("failed to persist inbound image; continuing without storage")

        llm = await active_llm(c.settings, c.config)
        if llm is not None:
            provider, model, api_key, _extra_params = llm
            try:
                description = await provider.describe_image(
                    media.bytes, media.mime_type, api_key, model, timeout=20.0
                )
            except ProviderError:
                logger.exception("vision description failed; continuing without it")

    parts: list[str] = []
    if event.caption:
        parts.append(event.caption)
    if description:
        parts.append(f"[Photo — appears to show: {description}]")
    if not parts:
        parts.append("[Customer sent a photo, but it could not be processed]")

    return InboundText(
        message_id=event.message_id,
        wa_id=event.wa_id,
        text="\n\n".join(parts),
        timestamp=event.timestamp,
    )
