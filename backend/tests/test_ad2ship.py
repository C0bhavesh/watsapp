import pathlib

import httpx
import pytest

from app.shopify.ad2ship import Ad2shipTracking, fetch_tracking

FIX = pathlib.Path(__file__).parent / "agents" / "fixtures" / "ad2ship"


def _client(
    html: str | None,
    status_code: int = 200,
    exc: type[Exception] | None = None,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc("boom")
        return httpx.Response(status_code, text=html or "")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_delivered_page_parses_as_customer_delivery() -> None:
    html = (FIX / "delivered_tavas4464.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "57143610479732")
    assert t is not None
    assert isinstance(t, Ad2shipTracking)
    assert t.status == "delivered"
    assert t.is_delivered_to_customer() and not t.is_rto()
    assert t.is_terminal()
    assert t.last_scan_remark == "Delivered"
    assert t.current_city == "Maharashtra"
    assert t.expected_date is None  # only a "Delivered Date" box present


@pytest.mark.parametrize(
    "fixture", sorted(FIX.glob("rto_delivered_*.html")), ids=lambda p: p.name
)
@pytest.mark.asyncio
async def test_rto_page_parses_as_rto(fixture: pathlib.Path) -> None:
    html = fixture.read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "x")
    assert t is not None and t.status == "rto_delivered"
    assert t.is_rto() and t.is_terminal() and not t.is_delivered_to_customer()
    assert (t.current_hub or "").startswith("Surat_")
    assert t.expected_date is None  # only a "Delivered Date" box present


@pytest.mark.asyncio
async def test_in_transit_page_is_non_terminal() -> None:
    html = next(FIX.glob("in_transit_*.html")).read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "x")
    assert t is not None and not t.is_terminal()
    assert t.status == "in_transit"
    assert t.expected_date == "Aug 29, 2026"  # "Expected Delivery" date-box
    assert t.current_hub == "Mumbai_AzadNagar_D"
    assert t.current_city == "Maharashtra"


@pytest.mark.asyncio
async def test_malformed_page_returns_none() -> None:
    html = (FIX / "malformed.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        assert await fetch_tracking(c, "x") is None


@pytest.mark.asyncio
async def test_http_error_returns_none() -> None:
    async with _client(None, status_code=500) as c:
        assert await fetch_tracking(c, "x") is None


@pytest.mark.asyncio
async def test_timeout_returns_none() -> None:
    async with _client(None, exc=httpx.TimeoutException) as c:
        assert await fetch_tracking(c, "x") is None


@pytest.mark.asyncio
async def test_awb_with_space_is_quoted_and_never_raises() -> None:
    # A free-text AWB with a space would make httpx raise InvalidURL if interpolated raw; quoting it
    # keeps the "never raises" contract. Against the delivered fixture the quoted request parses
    # normally rather than raising.
    html = (FIX / "delivered_tavas4464.html").read_text(encoding="utf-8")
    async with _client(html) as c:
        t = await fetch_tracking(c, "AWB 123 45")
    assert t is not None and t.status == "delivered"


@pytest.mark.asyncio
async def test_non_ascii_awb_never_raises() -> None:
    # A non-ASCII AWB also gets quoted; even if the page 404s, fetch_tracking returns None, never
    # raises httpx.InvalidURL.
    async with _client(None, status_code=404) as c:
        assert await fetch_tracking(c, "abcé123") is None
