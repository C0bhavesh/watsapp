import asyncio
import logging

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_inbound import InboundImage, InboundText
from app.channels.whatsapp_media import FetchedMedia, fetch_media
from app.config.crypto import VaultError
from app.core.phone import normalize_phone
from app.deps import Container, active_llm
from app.providers.base import ProviderError

logger = logging.getLogger("app.channels.whatsapp_image_intake")

_PHOTO_PLACEHOLDER = "[Customer sent a photo, but it could not be processed]"


async def _fetch_and_describe(
    c: Container, cfg: WhatsAppConfig, event: InboundImage, phone: str, call_timeout: float
) -> str | None:
    """Download, store, and vision-describe the image. Returns the description, or None on any
    failure. Storage and vision failures are independent -- a vision failure never undoes a
    successful store, and a storage failure never blocks the vision call."""
    media: FetchedMedia | None = await fetch_media(
        c.http, cfg, event.media_id, timeout=call_timeout
    )
    if media is None:
        return None

    try:
        await c.ingest.save_inbound_image(phone, event.message_id, media.mime_type, media.bytes)
    except Exception:
        logger.warning("failed to persist inbound image; continuing without storage")

    try:
        llm = await active_llm(c.settings, c.config)
    except VaultError:
        logger.warning("llm config unreadable; continuing without vision")
        llm = None
    if llm is None:
        return None

    provider, model, api_key, extra_params = llm
    try:
        return await provider.describe_image(
            media.bytes, media.mime_type, api_key, model, call_timeout, extra_params=extra_params
        )
    except ProviderError:
        logger.warning("vision description failed; continuing without it")
        return None


async def handle_inbound_image(
    c: Container, cfg: WhatsAppConfig, event: InboundImage, budget_seconds: float
) -> InboundText:
    """Turn an inbound image into a synthesized InboundText for run_turn.

    Downloads + stores the image and asks the active LLM to describe it (vision), then combines
    that description with the customer's caption (if any) into one plain-text message. Never
    raises: any failure (download, storage, vision, an exhausted time budget, or an unparseable
    wa_id) degrades to using the caption alone, or a fixed placeholder if there is no caption
    either, so an inbound image always produces SOME turn instead of silently vanishing.

    budget_seconds is the per-delivery time remaining when this call started. call_timeout caps
    each individual network call (fetch_media does up to 2 sequential HTTP calls, describe_image
    adds a third); asyncio.wait_for around the whole sequence ALSO enforces a genuine wall-clock
    total of 3x call_timeout, capped at 60% of budget_seconds so run_turn is always guaranteed
    some minimum share of the remaining delivery budget -- a bare timeout= float handed to httpx
    is four independent per-operation deadlines, never a wall-clock cap, so the per-call bound
    alone cannot guarantee this function returns in time (see error_learnings.md, 2026-08-14). If
    too little budget remains to safely attempt a network round trip, skip straight to the
    caption/placeholder fallback rather than risk exhausting the whole delivery.
    """
    phone = normalize_phone(event.wa_id)
    description: str | None = None

    if phone is not None:
        call_timeout = min(15.0, budget_seconds / 3)
        if call_timeout >= 2.0:
            wait_timeout = min(call_timeout * 3, budget_seconds * 0.6)
            try:
                description = await asyncio.wait_for(
                    _fetch_and_describe(c, cfg, event, phone, call_timeout),
                    timeout=wait_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "image intake timed out after %.1fs; continuing without it", wait_timeout
                )
        else:
            logger.warning(
                "image intake skipped: only %.1fs of the per-delivery budget remains",
                budget_seconds,
            )
    # else: an unparseable wa_id would store the image under a key find_inbound_images_by_phone
    # (keyed on the SAME normalize_phone(wa_id) form) could never look back up -- skip
    # storage/vision entirely and degrade straight to the caption/placeholder path below.

    parts: list[str] = []
    if event.caption:
        parts.append(event.caption)
    if description:
        parts.append(f"[Photo — appears to show: {description}]")
    if not parts:
        parts.append(_PHOTO_PLACEHOLDER)

    return InboundText(
        message_id=event.message_id,
        wa_id=event.wa_id,
        text="\n\n".join(parts),
        timestamp=event.timestamp,
    )
