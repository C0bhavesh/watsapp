import json
import re
from dataclasses import dataclass
from typing import Any

from app.providers.base import LLMProvider, Message
from app.shopify.models import AuthorizedOrder

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)

# Shared "Friendly Fashion Advisor" personality, injected into every agent's system prompt so
# tone stays consistent regardless of which specialist answers (client decision, round 3
# 2026-08-06). This is a cross-cutting constraint, not one agent's job.
PERSONALITY = """You are the Thetavas WhatsApp shopping assistant -- a Friendly Fashion \
Advisor, not just a support bot. Be warm, professional, and fashion-knowledgeable. Speak \
naturally in English, Hindi, or Hinglish, matching the customer's own language and style. \
Use emojis sparingly, only when they fit naturally. Be honest and trustworthy -- never invent \
product details, policy terms, or order information; if you don't know something, say so and \
offer to connect the customer with the team. Never state a customer's total spending, order \
count, or detailed purchase history unless they explicitly ask for it -- you may use that \
knowledge to inform your tone (for example, a warmer welcome-back for a returning customer) \
but never announce the numbers unprompted."""


@dataclass(frozen=True)
class AgentContext:
    wa_id: str
    phone_e164: str
    user_text: str
    history: list[Message]
    orders: list[AuthorizedOrder]
    is_vip: bool
    knowledge: dict[str, str]
    provider: LLMProvider
    model: str
    api_key: str
    extra_params: dict[str, object] | None
    timeout: float = 20.0


@dataclass(frozen=True)
class AgentReply:
    text: str
    handoff: bool = False


def extract_json_blob(raw_text: str) -> dict[str, object] | None:
    """Hardened JSON-object extraction from a raw LLM completion.

    Strips <think> reasoning blocks and ``` code-fence wrapping, then tries direct
    ``json.loads``, falling back to extracting the outermost ``{...}`` span. Returns None
    (never raises) if no JSON object can be recovered -- callers own their own fallback.
    """
    text = _THINK_RE.sub("", raw_text)
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text.strip()
    try:
        data: Any = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    brace_match = _BRACES_RE.search(candidate)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            return data if isinstance(data, dict) else None
        except ValueError:
            pass
    return None


def extract_reply_text(raw_text: str, fallback: str) -> str:
    """Extract the customer-facing reply text from a raw completion.

    Prefers the requested ``{"reply": "..."}`` JSON shape; if the completion parses as JSON
    but has no usable ``reply`` string, degrades to ``fallback`` (never leaks raw JSON syntax
    to the customer). If the completion isn't JSON at all, the plain text is trusted as-is --
    some models drift from the requested format but still produce a safe natural-language
    reply, and treating "wasn't JSON" as a hard failure would reject good replies.
    """
    data = extract_json_blob(raw_text)
    if data is not None:
        reply = data.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
        return fallback
    plain = raw_text.strip()
    return plain if plain else fallback
