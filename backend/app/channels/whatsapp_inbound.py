from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundText:
    message_id: str
    wa_id: str
    text: str
    timestamp: str


@dataclass(frozen=True)
class InboundInteractive:
    message_id: str
    wa_id: str
    button_id: str
    button_title: str
    timestamp: str


@dataclass(frozen=True)
class InboundButton:
    message_id: str
    wa_id: str
    payload: str
    button_text: str
    context_message_id: str | None
    timestamp: str


InboundEvent = InboundText | InboundInteractive | InboundButton


def extract_event(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse a Meta webhook envelope into a typed inbound event, or None.

    Every field is treated as attacker-typed: a malformed/type-confused payload
    yields None, never an exception. Template quick-reply taps (type "button")
    are a distinct variant from interactive button replies (type "interactive").
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None
        msg = messages[0]
    except (KeyError, IndexError, TypeError):
        return None

    message_id = msg.get("id")
    wa_id = msg.get("from")
    timestamp = msg.get("timestamp")
    if not isinstance(message_id, str) or not isinstance(wa_id, str):
        return None
    timestamp_str = str(timestamp) if timestamp is not None else ""
    msg_type = msg.get("type")

    if msg_type == "text":
        text = (msg.get("text") or {}).get("body")
        if not isinstance(text, str):
            return None
        return InboundText(
            message_id=message_id, wa_id=wa_id, text=text, timestamp=timestamp_str
        )

    if msg_type == "button":
        button = msg.get("button") or {}
        button_payload = button.get("payload")
        if not isinstance(button_payload, str):
            return None
        context_id = (msg.get("context") or {}).get("id")
        return InboundButton(
            message_id=message_id,
            wa_id=wa_id,
            payload=button_payload,
            button_text=str(button.get("text") or ""),
            context_message_id=context_id if isinstance(context_id, str) else None,
            timestamp=timestamp_str,
        )

    if msg_type == "interactive":
        interactive = msg.get("interactive") or {}
        if interactive.get("type") != "button_reply":
            return None
        reply = interactive.get("button_reply") or {}
        button_id = reply.get("id")
        if not isinstance(button_id, str):
            return None
        return InboundInteractive(
            message_id=message_id,
            wa_id=wa_id,
            button_id=button_id,
            button_title=str(reply.get("title") or ""),
            timestamp=timestamp_str,
        )

    return None
