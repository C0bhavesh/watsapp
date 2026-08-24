import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.channels.whatsapp_config import WhatsAppConfig

# WhatsApp's own inbound-image size ceiling; reject anything larger rather than pay to
# store/vision-analyze an oversized payload.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
# Meta's Graph API media-download URLs resolve to one of these domain families. Dot-anchored
# suffix match (mirrors shopify/client.py::_is_shopify_image_url) so a lookalike host
# ("fbsbx.com.evil.com") is rejected, not just a substring match.
_META_MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".fbcdn.net", ".facebook.com")

logger = logging.getLogger("app.channels.whatsapp_media")


@dataclass(frozen=True)
class FetchedMedia:
    bytes: bytes
    mime_type: str


def _is_trusted_meta_media_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = parts.hostname
    return host is not None and any(host.endswith(suffix) for suffix in _META_MEDIA_HOST_SUFFIXES)


async def fetch_media(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, media_id: str, timeout: float = 20.0
) -> FetchedMedia | None:
    """Resolve a Meta media id to its bytes, or None on any failure/rejection.

    Two Bearer-authenticated Graph API calls: first resolves media_id -> a short-lived download
    URL + Meta's own reported mime_type, second fetches the bytes from that URL. Never raises --
    every failure mode (network error, non-200, malformed JSON, disallowed mime type, untrusted
    host, oversized body) degrades to None, mirroring whatsapp_inbound.py's "attacker/network
    input, never raise" posture.
    """
    headers = {"Authorization": f"Bearer {cfg.access_token}"}
    try:
        meta_resp = await http.get(
            f"https://graph.facebook.com/{cfg.api_version}/{media_id}",
            headers=headers, timeout=timeout,
        )
    except httpx.HTTPError:
        return None
    if meta_resp.status_code != 200:
        return None
    try:
        meta = meta_resp.json()
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    url = meta.get("url")
    mime_type = meta.get("mime_type")
    if not isinstance(url, str) or not isinstance(mime_type, str):
        return None
    if mime_type not in _ALLOWED_IMAGE_MIME_TYPES:
        return None
    if not _is_trusted_meta_media_url(url):
        logger.warning(
            "rejected media download host %r (not in the Meta CDN allowlist)",
            urlsplit(url).hostname,
        )
        return None

    try:
        data_resp = await http.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return None
    if data_resp.status_code != 200:
        return None
    content = data_resp.content
    if len(content) > _MAX_IMAGE_BYTES:
        return None
    return FetchedMedia(bytes=content, mime_type=mime_type)
