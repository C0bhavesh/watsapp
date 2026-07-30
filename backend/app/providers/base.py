from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


class ProviderErrorKind(StrEnum):
    """Semantic category of a ProviderError — lets callers distinguish auth vs quota."""

    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class ProviderError(Exception):
    """Raised when the upstream LLM provider returns an error or unexpected response."""

    def __init__(
        self, message: str, kind: ProviderErrorKind = ProviderErrorKind.UNKNOWN
    ) -> None:
        super().__init__(message)
        self.kind: ProviderErrorKind = kind


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str


class LLMProvider(Protocol):
    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult: ...
