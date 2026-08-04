from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    label: str
    default_model: str
    # True: a 429 proves the key authenticated (provider validates before throttling) —
    # save it with a soft warning instead of rejecting.
    accept_on_rate_limit: bool = False
    # "api_key": credential is a single key string stored encrypted (entered in the admin
    # panel). "env": credential(s) come from environment variables (e.g. a Vertex AI
    # service-account JSON) — nothing is pasted or stored in the config DB.
    auth_kind: str = "api_key"
    # Extra kwargs spread into the litellm.acompletion call for this provider (e.g. Vertex's
    # temperature/reasoning_effort). None = no extras. A dict field makes the frozen instance
    # unhashable, which is fine: ProviderInfo objects are only stored as dict VALUES.
    request_params: dict[str, object] | None = None


# "gemini" = direct API-key auth. "vertex" = Gemini via Vertex AI with env-based
# service-account auth (VERTEX_CREDENTIALS_JSON / VERTEX_PROJECT / VERTEX_LOCATION),
# injected by the litellm adapter for vertex_ai/* models — no key is pasted or stored.
# temperature 0.3 keeps replies focused; reasoning_effort "medium" gives enough thinking budget.
PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo("gemini", "Gemini", "gemini/gemini-flash-latest"),
    "vertex": ProviderInfo(
        "vertex",
        "Gemini (Vertex AI)",
        "vertex_ai/gemini-3.5-flash",
        auth_kind="env",
        request_params={"temperature": 0.3, "reasoning_effort": "medium"},
    ),
}


def get_provider(key: str) -> ProviderInfo | None:
    return PROVIDERS.get(key)


def list_providers() -> list[ProviderInfo]:
    return list(PROVIDERS.values())
