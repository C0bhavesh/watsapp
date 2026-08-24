import httpx

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_media import fetch_media

CFG = WhatsAppConfig(
    access_token="tok", app_secret="sec", verify_token="vtok",
    phone_number_id="123", waba_id="456", api_version="v23.0",
)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _transport(media_response: dict, download_body: bytes, download_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=media_response)
        if str(request.url) == media_response.get("url"):
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(download_status, content=download_body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_fetch_media_returns_bytes_and_mime_type() -> None:
    transport = _transport(
        {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/img1",
            "mime_type": "image/jpeg",
        },
        b"\xff\xd8\xff\xe0fakejpeg",
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is not None
    assert result.mime_type == "image/jpeg"
    assert result.bytes == b"\xff\xd8\xff\xe0fakejpeg"


async def test_fetch_media_rejects_disallowed_mime_type() -> None:
    transport = _transport(
        {"url": "https://lookaside.fbsbx.com/x", "mime_type": "application/pdf"}, b"data"
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_rejects_untrusted_host() -> None:
    transport = _transport(
        {"url": "https://evil.example.com/x", "mime_type": "image/jpeg"}, b"data"
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_rejects_oversized_download() -> None:
    transport = _transport(
        {"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg"},
        b"x" * (5 * 1024 * 1024 + 1),
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_media_lookup_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_resolve_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_download_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            return httpx.Response(200, json={
                "url": "https://lookaside.fbsbx.com/x",
                "mime_type": "image/jpeg",
            })
        raise httpx.ReadError("connection reset")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_non_dict_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            return httpx.Response(200, json=["a", "list"])
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_missing_url_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            return httpx.Response(200, json={"mime_type": "image/jpeg"})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_url_wrong_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            return httpx.Response(200, json={
                "url": 123,
                "mime_type": "image/jpeg",
            })
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_download_non_200() -> None:
    transport = _transport(
        {"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg"},
        b"data",
        download_status=500,
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None
