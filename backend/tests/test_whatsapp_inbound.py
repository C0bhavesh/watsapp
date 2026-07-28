from app.channels.whatsapp_inbound import (
    InboundButton,
    InboundInteractive,
    InboundText,
    extract_event,
)


def envelope(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "2454816495000045",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1298805403309058"},
                    "messages": [message],
                },
            }],
        }],
    }


def test_text_message() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.TXT1", "timestamp": "1700000000",
        "type": "text", "text": {"body": "mera order kaha hai"},
    }))
    assert event == InboundText(
        message_id="wamid.TXT1", wa_id="919664290413",
        text="mera order kaha hai", timestamp="1700000000",
    )


def test_template_quick_reply_tap_is_inbound_button() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.BTN1", "timestamp": "1700000001",
        "type": "button",
        "context": {"id": "wamid.TEMPLATE_SENT"},
        "button": {"text": "Confirm Order", "payload": "order:confirm:gid://shopify/Order/1"},
    }))
    assert event == InboundButton(
        message_id="wamid.BTN1", wa_id="919664290413",
        payload="order:confirm:gid://shopify/Order/1", button_text="Confirm Order",
        context_message_id="wamid.TEMPLATE_SENT", timestamp="1700000001",
    )


def test_interactive_button_reply() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.INT1", "timestamp": "1700000002",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {
                "id": "order:cancel:confirm:gid://shopify/Order/1",
                "title": "Yes, cancel",
            },
        },
    }))
    assert event == InboundInteractive(
        message_id="wamid.INT1", wa_id="919664290413",
        button_id="order:cancel:confirm:gid://shopify/Order/1", button_title="Yes, cancel",
        timestamp="1700000002",
    )


def test_status_callback_is_none() -> None:
    payload = {
        "entry": [{"changes": [{"value": {
            "statuses": [{"id": "wamid.X", "status": "delivered"}],
        }}]}],
    }
    assert extract_event(payload) is None


def test_unknown_message_type_is_none() -> None:
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.IMG1", "timestamp": "1700000003",
        "type": "image", "image": {"id": "media123"},
    })) is None


def test_malformed_payload_is_none_not_exception() -> None:
    assert extract_event({}) is None
    assert extract_event({"entry": "not-a-list"}) is None
    assert extract_event(envelope({"type": "text"})) is None  # missing id/from/text


def test_type_confused_fields_are_none_not_exception() -> None:
    assert extract_event(envelope({
        "from": 919664290413, "id": None, "timestamp": 1700000000,
        "type": "text", "text": {"body": "hi"},
    })) is None
