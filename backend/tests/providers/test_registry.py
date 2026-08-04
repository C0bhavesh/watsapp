from app.providers.registry import get_provider, list_providers


def test_gemini_is_api_key_auth() -> None:
    p = get_provider("gemini")
    assert p is not None
    assert p.auth_kind == "api_key"


def test_vertex_is_env_auth_with_request_params() -> None:
    p = get_provider("vertex")
    assert p is not None
    assert p.label == "Gemini (Vertex AI)"
    assert p.default_model == "vertex_ai/gemini-3.5-flash"
    assert p.auth_kind == "env"
    assert p.request_params == {"temperature": 0.3, "reasoning_effort": "medium"}


def test_list_providers_returns_both() -> None:
    keys = {p.key for p in list_providers()}
    assert {"gemini", "vertex"} <= keys
