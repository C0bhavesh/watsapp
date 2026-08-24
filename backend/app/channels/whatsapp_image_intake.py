import logging

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_inbound import InboundImage, InboundText
from app.channels.whatsapp_media import FetchedMedia, fetch_media
from app.config.crypto import VaultError
from app.core.phone import normalize_phone
from app.deps import Container, active_llm
from app.providers.base import ProviderError

logger = logging.getLogger("app.channels.whatsapp_image_intake")


async def handle_inbound_image(
    c: Container, cfg: WhatsAppConfig, event: InboundImage, budget_seconds: float
) -> InboundText:
    """Turn an inbound image into a synthesized InboundText for run_turn.

    Downloads + stores the image and asks the active LLM to describe it (vision), then combines
    that description with the customer's caption (if any) into one plain-text message. Never
    raises: any failure (download, storage, vision, or an exhausted time budget) degrades to
    using the caption alone, or a fixed placeholder if there is no caption either, so an inbound
    image always produces SOME turn instead of silently vanishing.

    budget_seconds is the per-delivery time remaining when this call started -- fetch_media does
    up to 2 sequential HTTP calls and describe_image adds a third, so each is capped at a THIRD
    of the budget (bounded to a sane 15s ceiling) to leave room for the caller's own run_turn
    call afterward. If too little budget remains to safely attempt a network round trip, skip
    straight to the caption/placeholder fallback rather than risk exhausting the whole delivery.
    """
    phone = normalize_phone(event.wa_id)
    if phone is None:
        # An unparseable wa_id would store the image under a key find_inbound_images_by_phone
        # (keyed on the SAME normalize_phone(wa_id) form) could never look back up -- skip
        # storage/vision entirely and degrade to the caption/placeholder path below, same as any
        # other failure in this function.
        return InboundText(
            message_id=event.message_id,
            wa_id=event.wa_id,
            text=event.caption or "[Customer sent a photo, but it could not be processed]",
            timestamp=event.timestamp,
        )
    description: str | None = None

    call_timeout = min(15.0, budget_seconds / 3)
    media: FetchedMedia | None = None
    if call_timeout >= 2.0:
        media = await fetch_media(c.http, cfg, event.media_id, timeout=call_timeout)
    if media is not None:
        try:
            await c.ingest.save_inbound_image(
                phone, event.message_id, media.mime_type, media.bytes
            )
        except Exception:
            logger.exception("failed to persist inbound image; continuing without storage")

        try:
            llm = await active_llm(c.settings, c.config)
        except VaultError:
            logger.warning("llm config unreadable; continuing without vision")
            llm = None
        if llm is not None:
            provider, model, api_key, extra_params = llm
            try:
                description = await provider.describe_image(
                    media.bytes, media.mime_type, api_key, model, call_timeout,
                    extra_params=extra_params,
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
