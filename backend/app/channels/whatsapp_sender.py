import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.whatsapp_config import WhatsAppConfig

MAX_BUTTONS = 3
MAX_BUTTON_TITLE_LEN = 20

# Meta OAuth error bodies routinely echo the offending bearer token (e.g.
# "Invalid OAuth access token EAA..."). Never persist that verbatim.
_EAA_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]+")


def _safe_error(resp: httpx.Response, access_token: str) -> str:
    """Build a secret-free error string for a >=400 response.

    Keeps only Meta's structured error fields (code/type/error_subcode) plus the
    message with any token-shaped substring or the configured access token redacted.
    Falls back to a generic placeholder for non-JSON / unexpected shapes.
    """
    try:
        data = resp.json()
    except ValueError:
        return "non-JSON error response"
    if not isinstance(data, dict):
        return "non-JSON error response"
    err = data.get("error")
    if not isinstance(err, dict):
        return "non-JSON error response"
    parts: list[str] = []
    for key in ("code", "type", "error_subcode"):
        value = err.get(key)
        if isinstance(value, (str, int)):
            parts.append(f"{key}={value}")
    message = err.get("message")
    if isinstance(message, str):
        redacted = _EAA_TOKEN_RE.sub("[redacted]", message)
        if access_token:
            redacted = redacted.replace(access_token, "[redacted]")
        parts.append(f"message={redacted[:300]}")
    return "; ".join(parts) if parts else "non-JSON error response"


class WhatsAppSendError(Exception):
    """Raised on a transport failure (network/timeout) -- not a >=400 HTTP response."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status_code: int | None
    wamid: str | None
    error: str | None


def _messages_url(cfg: WhatsAppConfig) -> str:
    return f"https://graph.facebook.com/{cfg.api_version}/{cfg.phone_number_id}/messages"


async def _post_message(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, payload: dict[str, Any], timeout: float
) -> SendResult:
    headers = {
        "Authorization": f"Bearer {cfg.access_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = await http.post(
            _messages_url(cfg), json=payload, headers=headers, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise WhatsAppSendError(str(exc)) from exc
    if resp.status_code >= 400:
        return SendResult(
            ok=False,
            status_code=resp.status_code,
            wamid=None,
            error=_safe_error(resp, cfg.access_token),
        )
    wamid = _extract_wamid(resp)
    return SendResult(ok=True, status_code=resp.status_code, wamid=wamid, error=None)


def _extract_wamid(resp: httpx.Response) -> str | None:
    """Pull messages[0].id from a 2xx body, tolerating any unexpected shape.

    A malformed-but-2xx Meta response must degrade to wamid=None, never raise
    (a future outbox-drain cron would otherwise 500 on it).
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    wamid = first.get("id")
    return wamid if isinstance(wamid, str) else None


async def send_text(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, to: str, body: str, timeout: float = 20.0
) -> SendResult:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return await _post_message(http, cfg, payload, timeout)


async def send_template(
    http: httpx.AsyncClient,
    cfg: WhatsAppConfig,
    to: str,
    template_name: str,
    language: str,
    body_params: Mapping[str, str] | Sequence[str],
    button_payloads: Sequence[str] = (),
    header_image_url: str | None = None,
    timeout: float = 20.0,
) -> SendResult:
    """Send an approved template. ``body_params`` is either a NAMED mapping (name -> value, each
    parameter object carries ``parameter_name`` -- used by templates with named placeholders like
    ``cod_confirmation``) or a POSITIONAL sequence (used by templates with ``{{1}}``/``{{2}}``-style
    placeholders like ``cod_confirmmsg``/``cod_cancel`` -- no ``parameter_name`` key, order
    matters). ``header_image_url``, when a public https link, adds an IMAGE header component (the
    live product photo); omit it to send with no header. Components are ordered header -> body ->
    buttons, as Meta expects.
    """
    components: list[dict[str, Any]] = []
    # Defense-in-depth: only a public https link becomes an IMAGE header. The two callers
    # (ShopifyClient.get_product_image_url, outbox_drain.parse_payload) already https-check, but
    # this shared low-level sender must not forward an unvalidated URL straight to Meta — a missing
    # or non-https value degrades to "no header" (the send stays valid), never raises.
    if header_image_url and header_image_url.startswith("https://"):
        components.append(
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"link": header_image_url}}],
            }
        )
    if body_params:
        if isinstance(body_params, Mapping):
            parameters = [
                {"type": "text", "parameter_name": name, "text": value}
                for name, value in body_params.items()
            ]
        else:
            parameters = [{"type": "text", "text": value} for value in body_params]
        components.append({"type": "body", "parameters": parameters})
    for index, button_payload in enumerate(button_payloads):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": button_payload}],
            }
        )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }
    return await _post_message(http, cfg, payload, timeout)


async def send_buttons(
    http: httpx.AsyncClient,
    cfg: WhatsAppConfig,
    to: str,
    body_text: str,
    buttons: Sequence[tuple[str, str]],
    timeout: float = 20.0,
) -> SendResult:
    if not buttons or len(buttons) > MAX_BUTTONS:
        raise ValueError(f"send_buttons accepts 1-{MAX_BUTTONS} buttons, got {len(buttons)}")
    for _button_id, title in buttons:
        if len(title) > MAX_BUTTON_TITLE_LEN:
            raise ValueError(f"button title exceeds {MAX_BUTTON_TITLE_LEN} chars: {title!r}")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title}}
                    for bid, title in buttons
                ]
            },
        },
    }
    return await _post_message(http, cfg, payload, timeout)
