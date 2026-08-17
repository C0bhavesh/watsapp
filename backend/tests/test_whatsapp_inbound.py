import pytest

from app.channels.whatsapp_inbound import (
    InboundButton,
    InboundInteractive,
    InboundText,
    extract_event,
    extract_events,
    extract_statuses,
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


def test_extract_events_single_message() -> None:
    events = extract_events(envelope({
        "from": "919664290413", "id": "wamid.S1", "timestamp": "1",
        "type": "text", "text": {"body": "hi"},
    }))
    assert len(events) == 1
    assert isinstance(events[0], InboundText)
    assert events[0].message_id == "wamid.S1"


def test_extract_events_batch_of_two_keeps_both() -> None:
    # Meta can batch multiple messages into one delivery; none may be dropped.
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "1298805403309058"},
                    "messages": [
                        {"from": "919664290413", "id": "wamid.M1", "timestamp": "1",
                         "type": "text", "text": {"body": "cancel my order"}},
                        {"from": "919664290413", "id": "wamid.M2", "timestamp": "2",
                         "type": "text", "text": {"body": "please"}},
                    ],
                },
            }],
        }],
    }
    events = extract_events(payload)
    assert [e.message_id for e in events] == ["wamid.M1", "wamid.M2"]


def test_extract_events_across_multiple_entries_and_changes() -> None:
    payload = {
        "entry": [
            {"changes": [{"value": {"messages": [
                {"from": "91", "id": "wamid.E1", "timestamp": "1",
                 "type": "text", "text": {"body": "a"}},
            ]}}]},
            {"changes": [
                {"value": {"messages": [
                    {"from": "91", "id": "wamid.E2", "timestamp": "2",
                     "type": "text", "text": {"body": "b"}},
                ]}},
                {"value": {"messages": [
                    {"from": "91", "id": "wamid.E3", "timestamp": "3",
                     "type": "text", "text": {"body": "c"}},
                ]}},
            ]},
        ],
    }
    events = extract_events(payload)
    assert [e.message_id for e in events] == ["wamid.E1", "wamid.E2", "wamid.E3"]


def test_extract_events_skips_unparseable_messages_in_batch() -> None:
    # A batch mixing a valid text message with a status/unknown item keeps the valid one.
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "91", "id": "wamid.OK", "timestamp": "1",
             "type": "text", "text": {"body": "ok"}},
            "not-a-dict",
            {"from": "91", "id": "wamid.IMG", "timestamp": "2",
             "type": "image", "image": {"id": "m"}},
        ]}}]}],
    }
    events = extract_events(payload)
    assert [e.message_id for e in events] == ["wamid.OK"]


def test_extract_events_malformed_returns_empty_not_exception() -> None:
    assert extract_events({}) == []
    assert extract_events({"entry": "not-a-list"}) == []
    assert extract_events({"entry": [42]}) == []


@pytest.mark.parametrize("bad_message", ["not-a-dict", 42, ["nested", "list"], None, True])
def test_non_dict_messages_item_is_none_not_exception(bad_message: object) -> None:
    # messages[0] itself is not a dict — must not crash on msg.get(...).
    assert extract_event(envelope(bad_message)) is None


@pytest.mark.parametrize("bad_field", ["not-a-dict", 42, ["x"], True])
def test_non_dict_nested_button_is_none_not_exception(bad_field: object) -> None:
    # A truthy non-dict `button` must not crash on button.get("payload").
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.B", "timestamp": "1",
        "type": "button", "button": bad_field,
    })) is None


@pytest.mark.parametrize("bad_field", ["not-a-dict", 42, ["x"], True])
def test_non_dict_nested_interactive_is_none_not_exception(bad_field: object) -> None:
    # A truthy non-dict `interactive` must not crash on interactive.get("type").
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.I", "timestamp": "1",
        "type": "interactive", "interactive": bad_field,
    })) is None


@pytest.mark.parametrize("bad_field", ["not-a-dict", 42, ["x"], True])
def test_non_dict_nested_text_is_none_not_exception(bad_field: object) -> None:
    # A truthy non-dict `text` must not crash on text.get("body").
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.T", "timestamp": "1",
        "type": "text", "text": bad_field,
    })) is None


@pytest.mark.parametrize("bad_field", ["not-a-dict", 42, ["x"], True])
def test_non_dict_nested_button_reply_is_none_not_exception(bad_field: object) -> None:
    # A truthy non-dict `button_reply` must not crash on reply.get("id").
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.IR", "timestamp": "1",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": bad_field},
    })) is None


def test_extract_statuses_parses_a_delivered_event() -> None:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123456"},
                    "statuses": [{
                        "id": "wamid.ABC123",
                        "status": "delivered",
                        "timestamp": "1755500000",
                        "recipient_id": "919876543210",
                    }],
                }
            }]
        }]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert len(statuses) == 1
    assert statuses[0].wamid == "wamid.ABC123"
    assert statuses[0].status == "delivered"
    assert statuses[0].timestamp == "1755500000"


def test_extract_statuses_tenant_guard_rejects_mismatched_phone_number_id() -> None:
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "WRONG"},
            "statuses": [{"id": "wamid.X", "status": "read", "timestamp": "1"}],
        }}]}]
    }
    assert extract_statuses(payload, expected_phone_number_id="123456") == []


def test_extract_statuses_skips_malformed_entries_never_raises() -> None:
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123456"},
            "statuses": [
                {"id": "wamid.OK", "status": "sent", "timestamp": "1"},
                {"status": "read"},  # missing id -- unparseable, skipped
                "not a dict",         # malformed entry -- skipped
                {"id": "wamid.NOSTATUS"},  # missing status -- unparseable, skipped
            ],
        }}]}]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert len(statuses) == 1
    assert statuses[0].wamid == "wamid.OK"


def test_extract_statuses_multiple_entries_and_batched_statuses() -> None:
    payload = {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123456"},
            "statuses": [
                {"id": "wamid.A", "status": "sent", "timestamp": "1"},
                {"id": "wamid.B", "status": "delivered", "timestamp": "2"},
            ],
        }}]}]
    }
    statuses = extract_statuses(payload, expected_phone_number_id="123456")
    assert {s.wamid for s in statuses} == {"wamid.A", "wamid.B"}


def test_extract_statuses_no_statuses_key_returns_empty() -> None:
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "123456"}, "messages": [],
    }}]}]}
    assert extract_statuses(payload, expected_phone_number_id="123456") == []
