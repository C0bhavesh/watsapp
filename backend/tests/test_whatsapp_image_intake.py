import asyncio

import pytest

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_image_intake import handle_inbound_image
from app.channels.whatsapp_inbound import InboundImage, InboundText
from app.deps import get_container, reset_container

_CFG = WhatsAppConfig(
    access_token="test-token", app_secret="s", verify_token="v",
    phone_number_id="123", waba_id="456", api_version="v22.0",
)


@pytest.fixture(autouse=True)
def _reset(master_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # get_container() builds Settings() from env; APP_MASTER_KEY is required (no DATABASE_URL
    # here, so it falls back to the in-memory store, matching test_conversation.py's pattern).
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    yield
    reset_container()


async def test_handle_inbound_image_synthesizes_text_from_caption_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return intake.FetchedMedia(bytes=b"fakejpeg", mime_type="image/jpeg")

    async def fake_active_llm(settings, config):
        class _FakeProvider:
            async def describe_image(self, *a, **kw):
                return "a black cotton hoodie with a floral print"
        return (_FakeProvider(), "gemini/gemini-flash-latest", "test-key", None)

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)
    monkeypatch.setattr(intake, "active_llm", fake_active_llm)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG1", wa_id="919664290413", media_id="MEDIA1",
        mime_type="image/jpeg", caption="what's the price?", timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert isinstance(result, InboundText)
    assert result.message_id == "wamid.IMG1"
    assert result.wa_id == "919664290413"
    assert "what's the price?" in result.text
    assert "black cotton hoodie" in result.text

    saved = await c.ingest.find_inbound_images_by_phone("+919664290413")
    assert len(saved) == 1
    assert saved[0].mime_type == "image/jpeg"


async def test_handle_inbound_image_falls_back_to_caption_only_when_media_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return None

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG2", wa_id="919664290413", media_id="MEDIA2",
        mime_type="image/jpeg", caption="do you have this in blue?", timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert result.text.strip() == "do you have this in blue?"
    assert await c.ingest.find_inbound_images_by_phone("+919664290413") == []


async def test_handle_inbound_image_falls_back_to_placeholder_when_no_caption_and_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return None

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG3", wa_id="919664290413", media_id="MEDIA3",
        mime_type="image/jpeg", caption=None, timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert result.text.strip() != ""
    assert "photo" in result.text.lower()


async def test_handle_inbound_image_falls_back_to_caption_only_when_vision_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage and vision failures are independently handled: a vision-only failure still
    persists the image, and still yields a usable (caption-only) reply."""
    from app.channels import whatsapp_image_intake as intake
    from app.providers.base import ProviderError, ProviderErrorKind

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return intake.FetchedMedia(bytes=b"fakejpeg", mime_type="image/jpeg")

    async def fake_active_llm(settings, config):
        class _FakeProvider:
            async def describe_image(self, *a, **kw):
                raise ProviderError("vision failed", ProviderErrorKind.UNKNOWN)
        return (_FakeProvider(), "gemini/gemini-flash-latest", "test-key", None)

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)
    monkeypatch.setattr(intake, "active_llm", fake_active_llm)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG4", wa_id="919664290413", media_id="MEDIA4",
        mime_type="image/jpeg", caption="is this in stock?", timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert result.text.strip() == "is this in stock?"
    saved = await c.ingest.find_inbound_images_by_phone("+919664290413")
    assert len(saved) == 1


async def test_handle_inbound_image_falls_back_to_description_when_storage_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A storage failure must not block the vision description from reaching the reply."""
    from app.channels import whatsapp_image_intake as intake

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return intake.FetchedMedia(bytes=b"fakejpeg", mime_type="image/jpeg")

    async def fake_active_llm(settings, config):
        class _FakeProvider:
            async def describe_image(self, *a, **kw):
                return "a red canvas tote bag"
        return (_FakeProvider(), "gemini/gemini-flash-latest", "test-key", None)

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)
    monkeypatch.setattr(intake, "active_llm", fake_active_llm)

    c = get_container()

    async def failing_save_inbound_image(*a, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(c.ingest, "save_inbound_image", failing_save_inbound_image)

    event = InboundImage(
        message_id="wamid.IMG5", wa_id="919664290413", media_id="MEDIA5",
        mime_type="image/jpeg", caption="what's this?", timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert "red canvas tote bag" in result.text


async def test_handle_inbound_image_description_only_when_no_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    async def fake_fetch_media(http, cfg, media_id, timeout=20.0):
        return intake.FetchedMedia(bytes=b"fakejpeg", mime_type="image/jpeg")

    async def fake_active_llm(settings, config):
        class _FakeProvider:
            async def describe_image(self, *a, **kw):
                return "a pair of white sneakers"
        return (_FakeProvider(), "gemini/gemini-flash-latest", "test-key", None)

    monkeypatch.setattr(intake, "fetch_media", fake_fetch_media)
    monkeypatch.setattr(intake, "active_llm", fake_active_llm)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG6", wa_id="919664290413", media_id="MEDIA6",
        mime_type="image/jpeg", caption=None, timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert "white sneakers" in result.text
    assert "could not be processed" not in result.text


async def test_handle_inbound_image_wall_clock_timeout_cuts_off_a_stuck_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow/hung fetch_media must not blow past the wall-clock cap even though each
    individual httpx call also carries its own (independent) per-operation timeout -- proves
    the asyncio.wait_for wrapper, not just the per-call timeout= kwarg, is what actually bounds
    this function's total running time (error_learnings.md, 2026-08-14)."""
    from app.channels import whatsapp_image_intake as intake

    async def stuck_fetch_media(http, cfg, media_id, timeout=20.0):
        await asyncio.sleep(10)
        # unreachable within the 6.0s wait_for cap -- proves the wrapper cut it off before here
        return intake.FetchedMedia(bytes=b"never-reached", mime_type="image/jpeg")

    monkeypatch.setattr(intake, "fetch_media", stuck_fetch_media)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG7", wa_id="919664290413", media_id="MEDIA7",
        mime_type="image/jpeg", caption="still there?", timestamp="1700000000",
    )
    # budget_seconds=6.0 -> call_timeout = min(15, 6/3) = 2.0 (clears the >=2.0 floor, so the
    # sequence IS attempted) -> the wait_for wall-clock cap is call_timeout * 3 = 6.0s, well
    # under stuck_fetch_media's 10s sleep.
    result = await handle_inbound_image(c, _CFG, event, 6.0)

    assert result.text.strip() == "still there?"
    assert await c.ingest.find_inbound_images_by_phone("+919664290413") == []


async def test_handle_inbound_image_skips_network_when_budget_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    called = False

    async def unexpected_fetch_media(http, cfg, media_id, timeout=20.0):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(intake, "fetch_media", unexpected_fetch_media)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG8", wa_id="919664290413", media_id="MEDIA8",
        mime_type="image/jpeg", caption="quick question", timestamp="1700000000",
    )
    # budget_seconds=3.0 -> call_timeout = min(15, 3/3) = 1.0 < 2.0 floor -> fetch_media must
    # never even be attempted.
    result = await handle_inbound_image(c, _CFG, event, 3.0)

    assert called is False
    assert result.text.strip() == "quick question"
    assert await c.ingest.find_inbound_images_by_phone("+919664290413") == []


async def test_handle_inbound_image_unnormalizable_wa_id_skips_storage_and_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels import whatsapp_image_intake as intake

    called = False

    async def unexpected_fetch_media(http, cfg, media_id, timeout=20.0):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(intake, "fetch_media", unexpected_fetch_media)

    c = get_container()
    event = InboundImage(
        message_id="wamid.IMG9", wa_id="123", media_id="MEDIA9",
        mime_type="image/jpeg", caption="hi", timestamp="1700000000",
    )
    result = await handle_inbound_image(c, _CFG, event, 30.0)

    assert called is False
    assert result.text.strip() == "hi"
