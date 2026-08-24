import json
import sys

import pytest

from app.providers.base import Message, ProviderError, ProviderErrorKind
from app.providers.litellm_provider import LiteLLMProvider, VertexConfig


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeLiteLLM:
    """Stand-in for the lazily imported ``litellm`` module — captures acompletion kwargs."""

    def __init__(self) -> None:
        self.disable_aiohttp_transport = False
        self.captured: dict[str, object] | None = None

    async def acompletion(self, **kwargs: object) -> _FakeResp:
        self.captured = kwargs
        return _FakeResp("pong")


async def test_vertex_model_injects_vertex_creds_not_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    vertex = VertexConfig(credentials_json='{"sa":"json"}', project="proj", location="us-central1")
    provider = LiteLLMProvider(vertex=vertex)

    result = await provider.complete(
        "vertex_ai/gemini-3.5-flash",
        [Message("user", "ping")],
        "",  # no api_key for env-auth vertex
        15.0,
        extra_params={"temperature": 0.3},
    )

    assert result.text == "pong"
    kw = fake.captured
    assert kw is not None
    assert kw["vertex_credentials"] == '{"sa":"json"}'
    assert kw["vertex_project"] == "proj"
    assert kw["vertex_location"] == "us-central1"
    assert "api_key" not in kw  # vertex must NOT inject api_key
    assert kw["temperature"] == 0.3  # extra_params passthrough preserved


async def test_vertex_model_missing_creds_raises_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = LiteLLMProvider(vertex=None)

    with pytest.raises(ProviderError) as exc_info:
        await provider.complete(
            "vertex_ai/gemini-3.5-flash", [Message("user", "ping")], "", 15.0
        )

    assert exc_info.value.kind is ProviderErrorKind.AUTH
    assert fake.captured is None  # never reached litellm


async def test_vertex_error_never_surfaces_service_account_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vertex_ai/* error collapses to a fixed safe message — the raw text is discarded.

    Exact-substring redaction is not enough: litellm may reformat/re-serialize the
    credentials or echo a lone field, none of which match the stored string verbatim.
    """
    sa = {
        "client_email": "svc@proj-x.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----SECRETMATERIAL-----END PRIVATE KEY-----",
    }
    creds = json.dumps(sa)
    reformatted = json.dumps(sa, indent=2, sort_keys=True)  # same secret, different bytes
    lone_private_key = sa["private_key"]

    class _BoomLiteLLM:
        def __init__(self) -> None:
            self.disable_aiohttp_transport = False

        async def acompletion(self, **kwargs: object) -> _FakeResp:
            raise RuntimeError(
                f"vertex blew up with {creds} :: {reformatted} :: {lone_private_key}"
            )

    monkeypatch.setitem(sys.modules, "litellm", _BoomLiteLLM())
    vertex = VertexConfig(credentials_json=creds, project="proj", location="us-central1")
    provider = LiteLLMProvider(vertex=vertex)

    with pytest.raises(ProviderError) as exc_info:
        await provider.complete(
            "vertex_ai/gemini-3.5-flash", [Message("user", "ping")], "", 15.0
        )

    message = str(exc_info.value)
    assert message == "Vertex AI request failed"  # fixed safe message, kind preserved
    assert creds not in message
    assert reformatted not in message
    assert "private_key" not in message
    assert "client_email" not in message
    assert "BEGIN PRIVATE KEY" not in message


async def test_api_key_model_injects_api_key_not_vertex(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = LiteLLMProvider()  # no vertex config

    await provider.complete(
        "gemini/gemini-flash-latest", [Message("user", "ping")], "KEY123", 15.0
    )

    kw = fake.captured
    assert kw is not None
    assert kw["api_key"] == "KEY123"
    assert "vertex_credentials" not in kw


async def test_describe_image_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = LiteLLMProvider()

    result = await provider.describe_image(
        b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg", "test-key", "gemini/gemini-flash-latest", 20.0
    )

    assert result == "pong"  # _FakeLiteLLM.acompletion always returns _FakeResp("pong")
    kw = fake.captured
    assert kw is not None
    assert kw["model"] == "gemini/gemini-flash-latest"
    assert kw["api_key"] == "test-key"
    sent_messages = kw["messages"]
    assert isinstance(sent_messages, list)
    content = sent_messages[0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_describe_image_redacts_api_key_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingLiteLLM:
        disable_aiohttp_transport = False

        async def acompletion(self, **kwargs: object) -> object:
            raise RuntimeError("upstream said: bad key test-key-xyz")

    monkeypatch.setitem(sys.modules, "litellm", _RaisingLiteLLM())
    provider = LiteLLMProvider()

    with pytest.raises(ProviderError) as exc_info:
        await provider.describe_image(
            b"data", "image/jpeg", "test-key-xyz", "gemini/gemini-flash-latest", 20.0
        )
    assert "test-key-xyz" not in str(exc_info.value)
