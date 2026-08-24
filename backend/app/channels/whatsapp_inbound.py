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


@dataclass(frozen=True)
class InboundImage:
    message_id: str
    wa_id: str
    media_id: str
    mime_type: str
    caption: str | None
    timestamp: str


@dataclass(frozen=True)
class InboundStatus:
    wamid: str
    status: str
    timestamp: str
    # Meta's delivery-failure detail, populated only from a 'failed' status' errors[] array
    # (both None otherwise). error_code is Meta's numeric error code coerced to str (e.g.
    # "131047"); error_title is its short human label. Neither is PII/secret.
    error_code: str | None = None
    error_title: str | None = None


InboundEvent = InboundText | InboundInteractive | InboundButton | InboundImage


def extract_event(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse the FIRST inbound message of a Meta webhook envelope, or None.

    Retained for callers that only need one message. Prefer ``extract_events``
    for webhook handling: Meta can batch several messages into one delivery and
    everything after index 0 must not be dropped.
    """
    events = extract_events(payload)
    return events[0] if events else None


def extract_events(
    payload: dict[str, Any], expected_phone_number_id: str | None = None
) -> list[InboundEvent]:
    """Parse EVERY inbound message across all entries/changes into typed events.

    Every field is treated as attacker-typed: a malformed/type-confused payload
    or an unparseable individual message is skipped, never raised. A batched
    delivery (multiple messages in one webhook) yields one event per message so
    none is silently lost.

    When ``expected_phone_number_id`` is provided the tenant guard fails CLOSED:
    a change is processed only if its ``metadata.phone_number_id`` is present, a
    ``str``, and exactly equal to the expected id. Absent, wrong-typed, or
    mismatched metadata drops that change's messages entirely.
    """
    events: list[InboundEvent] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return events
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if expected_phone_number_id is not None and not _tenant_matches(
                value, expected_phone_number_id
            ):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for msg in messages:
                event = _parse_message(msg)
                if event is not None:
                    events.append(event)
    return events


def _tenant_matches(value: dict[str, Any], expected_phone_number_id: str) -> bool:
    """Fail-closed tenant check on a change's value.

    Requires metadata.phone_number_id to be present, a str, and exactly equal to
    the configured id. Anything else (absent, wrong type, mismatched) is rejected.
    """
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return False
    phone_id = metadata.get("phone_number_id")
    return isinstance(phone_id, str) and phone_id == expected_phone_number_id


def _parse_message(msg: Any) -> InboundEvent | None:
    """Parse a single message object into a typed event, or None if unparseable.

    Template quick-reply taps (type "button") are a distinct variant from
    interactive button replies (type "interactive").
    """
    if not isinstance(msg, dict):
        return None

    message_id = msg.get("id")
    wa_id = msg.get("from")
    timestamp = msg.get("timestamp")
    if not isinstance(message_id, str) or not isinstance(wa_id, str):
        return None
    timestamp_str = str(timestamp) if timestamp is not None else ""
    msg_type = msg.get("type")

    if msg_type == "text":
        text_obj = msg.get("text")
        text = text_obj.get("body") if isinstance(text_obj, dict) else None
        if not isinstance(text, str):
            return None
        return InboundText(
            message_id=message_id, wa_id=wa_id, text=text, timestamp=timestamp_str
        )

    if msg_type == "button":
        button = msg.get("button")
        if not isinstance(button, dict):
            return None
        button_payload = button.get("payload")
        if not isinstance(button_payload, str):
            return None
        context = msg.get("context")
        context_id = context.get("id") if isinstance(context, dict) else None
        return InboundButton(
            message_id=message_id,
            wa_id=wa_id,
            payload=button_payload,
            button_text=str(button.get("text") or ""),
            context_message_id=context_id if isinstance(context_id, str) else None,
            timestamp=timestamp_str,
        )

    if msg_type == "interactive":
        interactive = msg.get("interactive")
        if not isinstance(interactive, dict) or interactive.get("type") != "button_reply":
            return None
        reply = interactive.get("button_reply")
        if not isinstance(reply, dict):
            return None
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

    if msg_type == "image":
        image_obj = msg.get("image")
        if not isinstance(image_obj, dict):
            return None
        media_id = image_obj.get("id")
        mime_type = image_obj.get("mime_type")
        if not isinstance(media_id, str) or not isinstance(mime_type, str):
            return None
        caption = image_obj.get("caption")
        return InboundImage(
            message_id=message_id,
            wa_id=wa_id,
            media_id=media_id,
            mime_type=mime_type,
            caption=caption if isinstance(caption, str) else None,
            timestamp=timestamp_str,
        )

    return None


def extract_statuses(
    payload: dict[str, Any], expected_phone_number_id: str | None = None
) -> list[InboundStatus]:
    """Parse EVERY WhatsApp delivery/read status event across all entries/changes.

    Mirrors extract_events exactly (same tenant guard, same attacker-typed defensive parsing) but
    walks value.statuses instead of value.messages -- a distinct part of the same webhook envelope
    that this codebase otherwise ignores entirely.
    """
    statuses: list[InboundStatus] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return statuses
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if expected_phone_number_id is not None and not _tenant_matches(
                value, expected_phone_number_id
            ):
                continue
            raw_statuses = value.get("statuses")
            if not isinstance(raw_statuses, list):
                continue
            for raw in raw_statuses:
                status = _parse_status(raw)
                if status is not None:
                    statuses.append(status)
    return statuses


def _parse_status(raw: Any) -> InboundStatus | None:
    if not isinstance(raw, dict):
        return None
    wamid = raw.get("id")
    status = raw.get("status")
    if not isinstance(wamid, str) or not isinstance(status, str):
        return None
    timestamp = raw.get("timestamp")
    error_code, error_title = _parse_status_error(raw.get("errors"))
    return InboundStatus(
        wamid=wamid,
        status=status,
        timestamp=str(timestamp) if timestamp is not None else "",
        error_code=error_code,
        error_title=error_title,
    )


def _parse_status_error(errors: Any) -> tuple[str | None, str | None]:
    """Defensively pull (code, title) from a 'failed' status' errors[] array.

    Meta attaches errors[] (each with code/title/error_data.details) to a failed delivery. Every
    field is attacker-typed: only a non-empty list whose first element is a dict yields a value,
    and code/title are coerced to str (both None when absent). Anything else degrades to
    (None, None), never raises -- matching this file's other parsers.
    """
    if not isinstance(errors, list) or not errors:
        return None, None
    first = errors[0]
    if not isinstance(first, dict):
        return None, None
    code = first.get("code")
    title = first.get("title")
    return (
        str(code) if code is not None else None,
        str(title) if title is not None else None,
    )
