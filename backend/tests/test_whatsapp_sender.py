import json

import httpx
import pytest

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_sender import (
    SendResult,
    WhatsAppSendError,
    send_buttons,
    send_template,
    send_text,
)

CFG = WhatsAppConfig(
    access_token="tok", app_secret="sec", verify_token="vtok",
    phone_number_id="123", waba_id="456", api_version="v23.0",
)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_send_text_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://graph.facebook.com/v23.0/123/messages"
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.read())
        assert body == {
            "messaging_product": "whatsapp", "to": "919999999999",
            "type": "text", "text": {"body": "hi there"},
        }
        return httpx.Response(200, json={"messages": [{"id": "wamid.123"}]})

    result = await send_text(client_with(handler), CFG, "919999999999", "hi there")
    assert result == SendResult(ok=True, status_code=200, wamid="wamid.123", error=None)


async def test_send_text_4xx_is_not_ok_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    result = await send_text(client_with(handler), CFG, "919999999999", "hi")
    assert result.ok is False
    assert result.status_code == 401
    assert result.wamid is None


async def test_network_error_raises_whatsapp_send_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(WhatsAppSendError):
        await send_text(client_with(handler), CFG, "919999999999", "hi")


async def test_send_template_builds_named_body_and_button_components() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"messages": [{"id": "wamid.T1"}]})

    await send_template(
        client_with(handler), CFG, "919999999999", "cod_confirmation", "en",
        body_params={"customer_name": "Suman", "order_id": "tavas3733", "product_amount": "949"},
        button_payloads=["order:confirm:gid://1", "order:cancel:gid://1"],
    )
    template = captured["body"]["template"]
    assert template["name"] == "cod_confirmation"
    assert template["language"] == {"code": "en"}
    body_component = next(c for c in template["components"] if c["type"] == "body")
    # Named params: Meta requires parameter_name on each body parameter object for a named template.
    assert body_component["parameters"] == [
        {"type": "text", "parameter_name": "customer_name", "text": "Suman"},
        {"type": "text", "parameter_name": "order_id", "text": "tavas3733"},
        {"type": "text", "parameter_name": "product_amount", "text": "949"},
    ]
    button_components = [c for c in template["components"] if c["type"] == "button"]
    assert button_components[0]["index"] == "0"
    assert button_components[0]["parameters"][0]["payload"] == "order:confirm:gid://1"
    assert button_components[1]["index"] == "1"
    # No header image supplied -> no header component.
    assert not any(c["type"] == "header" for c in template["components"])


async def test_send_template_prepends_image_header_when_url_supplied() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"messages": [{"id": "wamid.T2"}]})

    await send_template(
        client_with(handler), CFG, "919999999999", "cod_confirmation", "en",
        body_params={"customer_name": "Suman"},
        button_payloads=["order:confirm:gid://1"],
        header_image_url="https://cdn.shopify.com/s/files/1/x.jpg",
    )
    components = captured["body"]["template"]["components"]
    header = next(c for c in components if c["type"] == "header")
    assert header["parameters"] == [
        {"type": "image", "image": {"link": "https://cdn.shopify.com/s/files/1/x.jpg"}}
    ]
    # Header comes before body, which comes before the buttons (Meta component ordering).
    assert [c["type"] for c in components] == ["header", "body", "button"]


async def test_send_template_drops_non_https_header_image() -> None:
    # Defense-in-depth: the shared low-level sender must not forward an unvalidated header URL to
    # Meta. Callers already https-check upstream, but a future caller passing a non-https URL must
    # degrade to "no header" (send stays valid), never raise, never reach Meta with a bad link.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"messages": [{"id": "wamid.T3"}]})

    result = await send_template(
        client_with(handler), CFG, "919999999999", "cod_confirmation", "en",
        body_params={"customer_name": "Suman"},
        button_payloads=["order:confirm:gid://1"],
        header_image_url="http://insecure.example/x.jpg",
    )
    assert result.ok is True
    components = captured["body"]["template"]["components"]
    # The non-https URL was dropped: no header component, body + button still present.
    assert not any(c["type"] == "header" for c in components)
    assert [c["type"] for c in components] == ["body", "button"]


async def test_error_body_does_not_leak_bearer_token() -> None:
    # Meta OAuth errors routinely echo the offending token in the message text.
    leaky = {
        "error": {
            "message": "Invalid OAuth access token EAAsecrettoken123456789",
            "type": "OAuthException",
            "code": 190,
            "error_subcode": 460,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=leaky)

    result = await send_text(client_with(handler), CFG, "919999999999", "hi")
    assert result.ok is False
    assert result.status_code == 401
    assert result.error is not None
    assert "EAAsecrettoken123456789" not in result.error
    # Structured code/type/subcode are safe to keep for diagnostics.
    assert "190" in result.error


async def test_error_body_redacts_configured_access_token() -> None:
    cfg = WhatsAppConfig(
        access_token="my-secret-access-token", app_secret="sec", verify_token="vtok",
        phone_number_id="123", waba_id="456", api_version="v23.0",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "bad token my-secret-access-token", "code": 100}}
        )

    result = await send_text(client_with(handler), cfg, "919999999999", "hi")
    assert result.error is not None
    assert "my-secret-access-token" not in result.error


async def test_non_json_error_body_falls_back_to_placeholder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error EAAtoken</html>")

    result = await send_text(client_with(handler), CFG, "919999999999", "hi")
    assert result.ok is False
    assert result.error == "non-JSON error response"


async def test_2xx_non_json_body_degrades_to_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    result = await send_text(client_with(handler), CFG, "919999999999", "hi")
    assert result == SendResult(ok=True, status_code=200, wamid=None, error=None)


@pytest.mark.parametrize(
    "body",
    [
        {"messages": "not-a-list"},
        {"messages": [1]},
        {"messages": [{}]},
        {},
        [],
    ],
)
async def test_2xx_unexpected_shape_degrades_to_ok(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    result = await send_text(client_with(handler), CFG, "919999999999", "hi")
    assert result.ok is True
    assert result.status_code == 200
    assert result.wamid is None
    assert result.error is None


async def test_send_buttons_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert body["interactive"]["type"] == "button"
        assert body["interactive"]["action"]["buttons"] == [
            {"type": "reply", "reply": {"id": "order:cancel:confirm:1", "title": "Yes, cancel"}},
            {"type": "reply", "reply": {"id": "order:cancel:abort:1", "title": "No, keep it"}},
        ]
        return httpx.Response(200, json={"messages": [{"id": "wamid.B1"}]})

    result = await send_buttons(
        client_with(handler), CFG, "919999999999", "Cancel this order?",
        [("order:cancel:confirm:1", "Yes, cancel"), ("order:cancel:abort:1", "No, keep it")],
    )
    assert result.ok is True


async def test_send_buttons_rejects_too_many() -> None:
    with pytest.raises(ValueError):
        await send_buttons(
            client_with(lambda r: httpx.Response(200, json={})), CFG, "9199", "x",
            [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")],
        )


async def test_send_buttons_rejects_long_title() -> None:
    with pytest.raises(ValueError):
        await send_buttons(
            client_with(lambda r: httpx.Response(200, json={})), CFG, "9199", "x",
            [("a", "This title is definitely way too long")],
        )
