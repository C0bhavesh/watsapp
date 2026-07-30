from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    label: str
    default_model: str
    # True: a 429 proves the key authenticated (provider validates before throttling) —
    # save it with a soft warning instead of rejecting.
    accept_on_rate_limit: bool = False


# v1 ships Gemini only (client direction). Vertex/env-auth providers are a Phase 4
# addition — the registry shape already supports them.
PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo("gemini", "Gemini", "gemini/gemini-flash-latest"),
}


def get_provider(key: str) -> ProviderInfo | None:
    return PROVIDERS.get(key)


def list_providers() -> list[ProviderInfo]:
    return list(PROVIDERS.values())
