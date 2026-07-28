from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.whatsapp_config import WhatsAppConfig

MAX_BUTTONS = 3
MAX_BUTTON_TITLE_LEN = 20


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
            ok=False, status_code=resp.status_code, wamid=None, error=resp.text[:500]
        )
    data = resp.json()
    wamid = (data.get("messages") or [{}])[0].get("id")
    return SendResult(ok=True, status_code=resp.status_code, wamid=wamid, error=None)


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
    body_params: Sequence[str],
    button_payloads: Sequence[str] = (),
    timeout: float = 20.0,
) -> SendResult:
    components: list[dict[str, Any]] = []
    if body_params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}
        )
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
