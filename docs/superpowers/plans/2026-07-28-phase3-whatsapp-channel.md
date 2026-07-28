# Phase 3 — WhatsApp Channel (the pipe) Implementation Plan

> **For agentic workers:** implement this plan task-by-task via the `developer` agent
> (TDD RED→GREEN→refactor per CLAUDE.md). After all tasks land: `code-reviewer`, then
> `security-reviewer` (webhook auth + secrets are a sensitive surface) per
> `.claude/rules/common/agents.md`. Steps use checkbox (`- [ ]`) syntax for tracking.
> (`superpowers:*` skills referenced in CLAUDE.md are not installed in this environment —
> this plan follows the same task structure as the Phase 1/2 plans instead.)

**Goal:** Build the WhatsApp transport "pipe" in both directions — Meta webhook receive
(GET verify + POST receive, **HEX** HMAC, dedupe, typed inbound parsing incl. `InboundButton`)
and send (`send_text`/`send_template`/`send_buttons`) — plus the deterministic multilingual
copy module. This is explicitly **only the pipe**: per the 2026-07-28 re-sequencing
(`docs/inbound-conversation-design.md`), routing a parsed event to the conversation engine
(Phase 4: providers/knowledge/engine/order_resolver) and executing button-tap mutations
(Phase 5: outbox drain, `tagsAdd`/`orderCancel`) are deliberately **not** built here. A
freshly-received, deduped event is acknowledged with its type and nothing else acts on it yet.

**Architecture:** New `app/channels/whatsapp_signature.py` (hex verifier, mirrors Shopify's
base64 one but must NOT be copy-pasted verbatim — see error_learnings), `whatsapp_config.py`
(Fernet-split config loader, mirrors the cafe's `_SECRET_FIELDS`/`_PLAIN_FIELDS` pattern over
our `ConfigService`), `whatsapp_inbound.py` (typed union incl. **new** `InboundButton` for
template quick-reply taps — F4 from the architecture review), `whatsapp_sender.py` (`send_text`
copied in spirit, `send_template`/`send_buttons` new), `copy.py` (fixed multilingual strings,
never LLM-generated). Store layer grows a `MessageStore` port (dedupe authority for
`processed_messages`, the Meta-side sibling of Phase 2's `processed_webhooks`).

**Tech Stack:** As Phase 1+2. No new dependencies.

## Global Constraints

- All Phase 1/2 Global Constraints still apply (secrets, ruff+mypy clean per task, secrets
  grep before each commit, Co-Authored-By trailer, **NEVER `git push`**).
- Meta webhook HMAC: header `X-Hub-Signature-256`, value `sha256=<hex>` — **HEX**, NOT
  base64 like Shopify (CLAUDE.md Critical Rule 3; error_learnings "Two different webhook
  HMAC schemes in one app"). Compare on the **raw body**, constant-time, ASCII-safe (encode
  both sides to bytes inside `try/except UnicodeEncodeError: return False` — error_learnings
  "hmac.compare_digest raises TypeError on non-ASCII str").
- Template quick-reply taps arrive as `type:"button"` with `button.{text,payload}` +
  `context.id` — **NOT** `type:"interactive"` (error_learnings "Template quick-reply taps
  are message type button, NOT interactive.button_reply"). `InboundButton` is a distinct
  variant from `InboundInteractive` (the latter is for buttons *we* sent via `send_buttons`,
  e.g. the cancel-confirm double-check that ships in Phase 5).
- Coerce every payload field before use — a 500 on a signed delivery is not acceptable (same
  Shopify-side lesson applies here even though Meta does not delete subscriptions on failure
  the way Shopify does; still, never crash on attacker-typed JSON).
- Secrets under config namespace `whatsapp:*`: `access_token`, `app_secret`, `verify_token`
  are Fernet-encrypted (`ConfigService.get_secret`/`set_secret`); `phone_number_id`,
  `waba_id`, `api_version` are plain (`get_plain`/`set_plain`) — mirrors the cafe project's
  `_SECRET_FIELDS`/`_PLAIN_FIELDS` split. All six required — `load_whatsapp_config` returns
  `None` (fail closed) if any is unset, no hardcoded `api_version` default (ADR-005: client/
  operational values are config, not code).
- Out of scope for Phase 3 (do not build): `core/order_resolver.py`, `core/engine.py`,
  `app/knowledge/`, conversation/message persistence, deterministic button-tap→mutation
  dispatch, outbox integration. These attach in Phases 4–5 without restructuring this pipe.
- `processed_messages(message_id PK, received_at)` table already exists (Phase 2 schema.sql,
  Level 4) — this phase adds the repo code for it, not the table.

## File Structure (Phase 3 additions)

```
backend/
  app/channels/whatsapp_signature.py   # verify_meta_hmac (hex, constant-time)
  app/channels/whatsapp_config.py      # WhatsAppConfig + load_whatsapp_config
  app/channels/whatsapp_inbound.py     # InboundText/InboundInteractive/InboundButton + extract_event
  app/channels/whatsapp_sender.py      # send_text/send_template/send_buttons + SendResult
  app/channels/copy.py                 # deterministic multilingual reply strings
  app/channels/whatsapp.py             # GET verify + POST receive router
  app/store/base.py                    # + MessageStore Protocol (modify)
  app/store/memory.py                  # + InMemoryMessageStore (modify)
  app/store/postgres.py                # + PostgresMessageStore (modify)
  app/deps.py                          # + Container.messages (modify)
  app/main.py                          # include whatsapp router (modify)
  scripts/smoke_whatsapp.py            # live send_text smoke check
  tests/test_whatsapp_signature.py
  tests/test_whatsapp_config.py
  tests/test_whatsapp_inbound.py
  tests/test_message_store.py
  tests/test_whatsapp_sender.py
  tests/test_copy.py
  tests/test_whatsapp_webhook.py
  tests/test_postgres_message_store.py  # skipif no TEST_DATABASE_URL
```

---

### Task 1: Meta HEX HMAC verifier

**Files:**
- Create: `backend/app/channels/whatsapp_signature.py`
- Test: `backend/tests/test_whatsapp_signature.py`

**Interfaces:**
- Produces: `verify_meta_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_whatsapp_signature.py`:
```python
import hashlib
import hmac as hmac_lib

from app.channels.whatsapp_signature import verify_meta_hmac

SECRET = "app-secret"
BODY = b'{"entry": []}'


def good_header() -> str:
    digest = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes() -> None:
    assert verify_meta_hmac(BODY, good_header(), SECRET)


def test_tampered_body_fails() -> None:
    assert not verify_meta_hmac(b'{"entry": [1]}', good_header(), SECRET)


def test_missing_header_fails() -> None:
    assert not verify_meta_hmac(BODY, None, SECRET)
    assert not verify_meta_hmac(BODY, "", SECRET)


def test_missing_prefix_fails() -> None:
    digest = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert not verify_meta_hmac(BODY, digest, SECRET)  # bare hex, no "sha256=" prefix


def test_base64_encoding_is_rejected() -> None:
    import base64

    b64 = base64.b64encode(hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()
    assert not verify_meta_hmac(BODY, f"sha256={b64}", SECRET)  # Shopify's scheme, not Meta's


def test_non_ascii_header_fails_closed() -> None:
    assert not verify_meta_hmac(BODY, "sha256=\xe9\xe9", SECRET)
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/whatsapp_signature.py`:
```python
import hashlib
import hmac

_PREFIX = "sha256="


def verify_meta_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """Meta webhook HMAC: sha256=<hex>(HMAC-SHA256(raw body, app secret)) -- NOT base64 like Shopify."""
    if not header_value or not header_value.startswith(_PREFIX):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    candidate = header_value[len(_PREFIX) :].strip()
    try:
        provided = candidate.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided)
```

- [ ] **Step 4: Run to verify PASS** — tests green; `ruff check .`; `mypy app` clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Meta hex HMAC verifier (constant-time, raw body)"`

---

### Task 2: WhatsApp config loader (Fernet split)

**Files:**
- Create: `backend/app/channels/whatsapp_config.py`
- Test: `backend/tests/test_whatsapp_config.py`

**Interfaces:**
- Consumes: `ConfigService` (Phase 1).
- Produces: `WhatsAppConfig` frozen dataclass (`access_token, app_secret, verify_token,
  phone_number_id, waba_id, api_version`); `WHATSAPP_SECRET_FIELDS`, `WHATSAPP_PLAIN_FIELDS`
  tuples; `async load_whatsapp_config(config: ConfigService) -> WhatsAppConfig | None`
  (`None` if any of the six keys is unset).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_whatsapp_config.py`:
```python
import pytest

from app.channels.whatsapp_config import WhatsAppConfig, load_whatsapp_config
from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.store.memory import InMemoryConfigRepo


async def _seeded_service(master_key: str) -> ConfigService:
    service = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await service.set_secret("whatsapp:access_token", "tok")
    await service.set_secret("whatsapp:app_secret", "sec")
    await service.set_secret("whatsapp:verify_token", "vtok")
    await service.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await service.set_plain("whatsapp:waba_id", "2454816495000045")
    await service.set_plain("whatsapp:api_version", "v23.0")
    return service


async def test_fully_configured_loads(master_key: str) -> None:
    service = await _seeded_service(master_key)
    cfg = await load_whatsapp_config(service)
    assert cfg == WhatsAppConfig(
        access_token="tok",
        app_secret="sec",
        verify_token="vtok",
        phone_number_id="1298805403309058",
        waba_id="2454816495000045",
        api_version="v23.0",
    )


@pytest.mark.parametrize(
    "missing_key",
    [
        "whatsapp:access_token",
        "whatsapp:app_secret",
        "whatsapp:verify_token",
        "whatsapp:phone_number_id",
        "whatsapp:waba_id",
        "whatsapp:api_version",
    ],
)
async def test_missing_any_field_returns_none(master_key: str, missing_key: str) -> None:
    service = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    seeded = await _seeded_service(master_key)
    for key in (
        "whatsapp:access_token", "whatsapp:app_secret", "whatsapp:verify_token",
        "whatsapp:phone_number_id", "whatsapp:waba_id", "whatsapp:api_version",
    ):
        if key == missing_key:
            continue
        value = await (seeded.get_secret(key) if "token" in key or "secret" in key else seeded.get_plain(key))
        assert value is not None
        if "token" in key or key == "whatsapp:app_secret":
            await service.set_secret(key, value)
        else:
            await service.set_plain(key, value)
    assert await load_whatsapp_config(service) is None
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/whatsapp_config.py`:
```python
from dataclasses import dataclass

from app.config.service import ConfigService

WHATSAPP_SECRET_FIELDS = ("access_token", "app_secret", "verify_token")
WHATSAPP_PLAIN_FIELDS = ("phone_number_id", "waba_id", "api_version")


@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str
    app_secret: str
    verify_token: str
    phone_number_id: str
    waba_id: str
    api_version: str


async def load_whatsapp_config(config: ConfigService) -> WhatsAppConfig | None:
    secrets = {f: await config.get_secret(f"whatsapp:{f}") for f in WHATSAPP_SECRET_FIELDS}
    plains = {f: await config.get_plain(f"whatsapp:{f}") for f in WHATSAPP_PLAIN_FIELDS}
    values = {**secrets, **plains}
    if any(not v for v in values.values()):
        return None
    return WhatsAppConfig(
        access_token=str(values["access_token"]),
        app_secret=str(values["app_secret"]),
        verify_token=str(values["verify_token"]),
        phone_number_id=str(values["phone_number_id"]),
        waba_id=str(values["waba_id"]),
        api_version=str(values["api_version"]),
    )
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: WhatsApp config loader (Fernet-split secrets/plain)"`

---

### Task 3: Typed inbound event parser (incl. `InboundButton`)

**Files:**
- Create: `backend/app/channels/whatsapp_inbound.py`
- Test: `backend/tests/test_whatsapp_inbound.py`

**Interfaces:**
- Produces: `InboundText(message_id, wa_id, text, timestamp)`,
  `InboundInteractive(message_id, wa_id, button_id, button_title, timestamp)`,
  `InboundButton(message_id, wa_id, payload, button_text, context_message_id, timestamp)`
  (all frozen dataclasses); `InboundEvent = InboundText | InboundInteractive | InboundButton`;
  `extract_event(payload: dict) -> InboundEvent | None`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_whatsapp_inbound.py`:
```python
from app.channels.whatsapp_inbound import (
    InboundButton,
    InboundInteractive,
    InboundText,
    extract_event,
)


def envelope(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "2454816495000045",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1298805403309058"},
                    "messages": [message],
                },
            }],
        }],
    }


def test_text_message() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.TXT1", "timestamp": "1700000000",
        "type": "text", "text": {"body": "mera order kaha hai"},
    }))
    assert event == InboundText(
        message_id="wamid.TXT1", wa_id="919664290413",
        text="mera order kaha hai", timestamp="1700000000",
    )


def test_template_quick_reply_tap_is_inbound_button() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.BTN1", "timestamp": "1700000001",
        "type": "button",
        "context": {"id": "wamid.TEMPLATE_SENT"},
        "button": {"text": "Confirm Order", "payload": "order:confirm:gid://shopify/Order/1"},
    }))
    assert event == InboundButton(
        message_id="wamid.BTN1", wa_id="919664290413",
        payload="order:confirm:gid://shopify/Order/1", button_text="Confirm Order",
        context_message_id="wamid.TEMPLATE_SENT", timestamp="1700000001",
    )


def test_interactive_button_reply() -> None:
    event = extract_event(envelope({
        "from": "919664290413", "id": "wamid.INT1", "timestamp": "1700000002",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "order:cancel:confirm:gid://shopify/Order/1", "title": "Yes, cancel"},
        },
    }))
    assert event == InboundInteractive(
        message_id="wamid.INT1", wa_id="919664290413",
        button_id="order:cancel:confirm:gid://shopify/Order/1", button_title="Yes, cancel",
        timestamp="1700000002",
    )


def test_status_callback_is_none() -> None:
    payload = {
        "entry": [{"changes": [{"value": {
            "statuses": [{"id": "wamid.X", "status": "delivered"}],
        }}]}],
    }
    assert extract_event(payload) is None


def test_unknown_message_type_is_none() -> None:
    assert extract_event(envelope({
        "from": "919664290413", "id": "wamid.IMG1", "timestamp": "1700000003",
        "type": "image", "image": {"id": "media123"},
    })) is None


def test_malformed_payload_is_none_not_exception() -> None:
    assert extract_event({}) is None
    assert extract_event({"entry": "not-a-list"}) is None
    assert extract_event(envelope({"type": "text"})) is None  # missing id/from/text


def test_type_confused_fields_are_none_not_exception() -> None:
    assert extract_event(envelope({
        "from": 919664290413, "id": None, "timestamp": 1700000000,
        "type": "text", "text": {"body": "hi"},
    })) is None
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/whatsapp_inbound.py`:
```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundText:
    message_id: str
    wa_id: str
    text: str
    timestamp: str


@dataclass(frozen=True)
class InboundInteractive:
    message_id: str
    wa_id: str
    button_id: str
    button_title: str
    timestamp: str


@dataclass(frozen=True)
class InboundButton:
    message_id: str
    wa_id: str
    payload: str
    button_text: str
    context_message_id: str | None
    timestamp: str


InboundEvent = InboundText | InboundInteractive | InboundButton


def extract_event(payload: dict[str, Any]) -> InboundEvent | None:
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None
        msg = messages[0]
    except (KeyError, IndexError, TypeError):
        return None

    message_id = msg.get("id")
    wa_id = msg.get("from")
    timestamp = msg.get("timestamp")
    if not isinstance(message_id, str) or not isinstance(wa_id, str):
        return None
    timestamp_str = str(timestamp) if timestamp is not None else ""
    msg_type = msg.get("type")

    if msg_type == "text":
        text = (msg.get("text") or {}).get("body")
        if not isinstance(text, str):
            return None
        return InboundText(message_id=message_id, wa_id=wa_id, text=text, timestamp=timestamp_str)

    if msg_type == "button":
        button = msg.get("button") or {}
        button_payload = button.get("payload")
        if not isinstance(button_payload, str):
            return None
        context_id = (msg.get("context") or {}).get("id")
        return InboundButton(
            message_id=message_id,
            wa_id=wa_id,
            payload=button_payload,
            button_text=str(button.get("text") or ""),
            context_message_id=context_id if isinstance(context_id, str) else None,
            timestamp=timestamp_str,
        )

    if msg_type == "interactive":
        interactive = msg.get("interactive") or {}
        if interactive.get("type") != "button_reply":
            return None
        reply = interactive.get("button_reply") or {}
        button_id = reply.get("id")
        if not isinstance(button_id, str):
            return None
        return InboundInteractive(
            message_id=message_id,
            wa_id=wa_id,
            button_id=button_id,
            button_title=str(reply.get("title") or ""),
            timestamp=timestamp_str,
        )

    return None
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: typed WhatsApp inbound event parser incl. InboundButton (F4)"`

---

### Task 4: MessageStore port + in-memory implementation

**Files:**
- Modify: `backend/app/store/base.py`, `backend/app/store/memory.py`
- Test: `backend/tests/test_message_store.py`

**Interfaces:**
- Produces (append to `base.py`): `MessageStore` Protocol —
  `async record_if_new(message_id: str) -> bool` (`True` iff newly recorded).
- Produces (`memory.py`): `InMemoryMessageStore()` with inspectable `.seen: set[str]`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_message_store.py`:
```python
from app.store.memory import InMemoryMessageStore


async def test_first_time_message_is_new() -> None:
    store = InMemoryMessageStore()
    assert await store.record_if_new("wamid.1") is True
    assert "wamid.1" in store.seen


async def test_replayed_message_is_not_new() -> None:
    store = InMemoryMessageStore()
    await store.record_if_new("wamid.1")
    assert await store.record_if_new("wamid.1") is False
```

- [ ] **Step 2: Run to verify FAIL** — `ImportError`

- [ ] **Step 3: Implement**

Append to `backend/app/store/base.py`:
```python
class MessageStore(Protocol):
    async def record_if_new(self, message_id: str) -> bool: ...
```

Append to `backend/app/store/memory.py`:
```python
class InMemoryMessageStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def record_if_new(self, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: MessageStore port with in-memory impl (Meta message dedupe authority)"`

---

### Task 5: Sender module — `send_text`/`send_template`/`send_buttons`

**Files:**
- Create: `backend/app/channels/whatsapp_sender.py`
- Test: `backend/tests/test_whatsapp_sender.py`

**Interfaces:**
- Consumes: `WhatsAppConfig` (Task 2).
- Produces: `SendResult(ok, status_code, wamid, error)` frozen dataclass;
  `WhatsAppSendError(Exception)`; `async send_text(http, cfg, to, body, timeout=20.0) -> SendResult`;
  `async send_template(http, cfg, to, template_name, language, body_params, button_payloads=(), timeout=20.0) -> SendResult`;
  `async send_buttons(http, cfg, to, body_text, buttons, timeout=20.0) -> SendResult`
  (`buttons: Sequence[tuple[id, title]]`, max 3, title ≤20 chars, else `ValueError`).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_whatsapp_sender.py`:
```python
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


async def test_send_template_builds_body_and_button_components() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"messages": [{"id": "wamid.T1"}]})

    await send_template(
        client_with(handler), CFG, "919999999999", "order_confirmation_cod", "hi",
        body_params=["Suman", "tavas3733", "949"],
        button_payloads=["order:confirm:gid://1", "order:cancel:gid://1"],
    )
    template = captured["body"]["template"]
    assert template["name"] == "order_confirmation_cod"
    assert template["language"] == {"code": "hi"}
    body_component = next(c for c in template["components"] if c["type"] == "body")
    assert [p["text"] for p in body_component["parameters"]] == ["Suman", "tavas3733", "949"]
    button_components = [c for c in template["components"] if c["type"] == "button"]
    assert button_components[0]["index"] == "0"
    assert button_components[0]["parameters"][0]["payload"] == "order:confirm:gid://1"
    assert button_components[1]["index"] == "1"


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
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/whatsapp_sender.py`:
```python
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from app.channels.whatsapp_config import WhatsAppConfig

MAX_BUTTONS = 3
MAX_BUTTON_TITLE_LEN = 20


class WhatsAppSendError(Exception):
    """Raised on a transport failure (network/timeout) -- not a >=400 HTTP response."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status_code: int | None
    wamid: str | None
    error: str | None


def _messages_url(cfg: WhatsAppConfig) -> str:
    return f"https://graph.facebook.com/{cfg.api_version}/{cfg.phone_number_id}/messages"


async def _post_message(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, payload: dict, timeout: float
) -> SendResult:
    headers = {"Authorization": f"Bearer {cfg.access_token}", "Content-Type": "application/json"}
    try:
        resp = await http.post(_messages_url(cfg), json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise WhatsAppSendError(str(exc)) from exc
    if resp.status_code >= 400:
        return SendResult(ok=False, status_code=resp.status_code, wamid=None, error=resp.text[:500])
    data = resp.json()
    wamid = (data.get("messages") or [{}])[0].get("id")
    return SendResult(ok=True, status_code=resp.status_code, wamid=wamid, error=None)


async def send_text(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, to: str, body: str, timeout: float = 20.0
) -> SendResult:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    return await _post_message(http, cfg, payload, timeout)


async def send_template(
    http: httpx.AsyncClient,
    cfg: WhatsAppConfig,
    to: str,
    template_name: str,
    language: str,
    body_params: Sequence[str],
    button_payloads: Sequence[str] = (),
    timeout: float = 20.0,
) -> SendResult:
    components: list[dict] = []
    if body_params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}
        )
    for index, button_payload in enumerate(button_payloads):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": button_payload}],
            }
        )
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template_name, "language": {"code": language}, "components": components},
    }
    return await _post_message(http, cfg, payload, timeout)


async def send_buttons(
    http: httpx.AsyncClient,
    cfg: WhatsAppConfig,
    to: str,
    body_text: str,
    buttons: Sequence[tuple[str, str]],
    timeout: float = 20.0,
) -> SendResult:
    if not buttons or len(buttons) > MAX_BUTTONS:
        raise ValueError(f"send_buttons accepts 1-{MAX_BUTTONS} buttons, got {len(buttons)}")
    for _button_id, title in buttons:
        if len(title) > MAX_BUTTON_TITLE_LEN:
            raise ValueError(f"button title exceeds {MAX_BUTTON_TITLE_LEN} chars: {title!r}")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title}} for bid, title in buttons
                ]
            },
        },
    }
    return await _post_message(http, cfg, payload, timeout)
```

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: WhatsApp sender - send_text/send_template/send_buttons"`

---

### Task 6: Deterministic multilingual copy module

**Files:**
- Create: `backend/app/channels/copy.py`
- Test: `backend/tests/test_copy.py`

**Interfaces:**
- Produces: `SUPPORTED_LANGUAGES = ("en", "hi", "hinglish", "gu")`;
  `copy_for(key: str, language: str) -> str` (falls back to `"en"` for an unsupported
  language; raises `KeyError` for an unknown key — call sites are internal, not user input).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_copy.py`:
```python
import pytest

from app.channels.copy import SUPPORTED_LANGUAGES, copy_for

KEYS = (
    "order_confirmed",
    "cancel_confirm_prompt",
    "order_cancelled",
    "order_not_found",
    "refusal_other_order",
    "error_fallback",
)


@pytest.mark.parametrize("key", KEYS)
def test_every_key_has_every_language(key: str) -> None:
    for language in SUPPORTED_LANGUAGES:
        text = copy_for(key, language)
        assert isinstance(text, str) and text.strip()


def test_unsupported_language_falls_back_to_english() -> None:
    assert copy_for("order_confirmed", "ta") == copy_for("order_confirmed", "en")


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        copy_for("no_such_key", "en")


def test_no_emojis_in_any_copy() -> None:
    for key in KEYS:
        for language in SUPPORTED_LANGUAGES:
            text = copy_for(key, language)
            assert all(ord(ch) < 0x1F000 for ch in text), (key, language)
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/copy.py`:
```python
from typing import Final

SUPPORTED_LANGUAGES: Final = ("en", "hi", "hinglish", "gu")
DEFAULT_LANGUAGE: Final = "en"

_COPY: Final[dict[str, dict[str, str]]] = {
    "order_confirmed": {
        "en": "Thank you, your order has been confirmed. We will ship it soon.",
        "hi": "धन्यवाद, आपका ऑर्डर कन्फर्म हो गया है। हम इसे जल्द भेजेंगे।",
        "hinglish": "Thank you, aapka order confirm ho gaya hai. Hum ise jaldi ship karenge.",
        "gu": "આભાર, તમારો ઓર્ડર કન્ફર્મ થઈ ગયો છે. અમે તેને જલ્દી મોકલીશું.",
    },
    "cancel_confirm_prompt": {
        "en": "Are you sure you want to cancel this order? This cannot be undone.",
        "hi": "क्या आप वाकई यह ऑर्डर कैंसल करना चाहते हैं? इसे बाद में वापस नहीं किया जा सकता।",
        "hinglish": "Kya aap sach mein yeh order cancel karna chahte hain? Yeh baad mein wapas nahi hoga.",
        "gu": "શું તમે ખરેખર આ ઓર્ડર કેન્સલ કરવા માંગો છો? આ પછીથી પાછું નહીં થાય.",
    },
    "order_cancelled": {
        "en": "Your order has been cancelled as requested.",
        "hi": "आपके अनुरोध पर आपका ऑर्डर कैंसल कर दिया गया है।",
        "hinglish": "Aapke request par order cancel kar diya gaya hai.",
        "gu": "તમારી વિનંતી મુજબ તમારો ઓર્ડર કેન્સલ કરવામાં આવ્યો છે.",
    },
    "order_not_found": {
        "en": "We could not find an order linked to this number. Could you share your order number?",
        "hi": "इस नंबर से जुड़ा कोई ऑर्डर नहीं मिला। कृपया अपना ऑर्डर नंबर बताएं।",
        "hinglish": "Is number se koi order nahi mila. Please apna order number bataiye.",
        "gu": "આ નંબર સાથે જોડાયેલો કોઈ ઓર્ડર મળ્યો નથી. કૃપા કરીને તમારો ઓર્ડર નંબર જણાવો.",
    },
    "refusal_other_order": {
        "en": "This order is not linked to your number, so we cannot share its details.",
        "hi": "यह ऑर्डर आपके नंबर से जुड़ा नहीं है, इसलिए हम इसकी जानकारी साझा नहीं कर सकते।",
        "hinglish": "Yeh order aapke number se linked nahi hai, isliye hum details share nahi kar sakte.",
        "gu": "આ ઓર્ડર તમારા નંબર સાથે જોડાયેલો નથી, તેથી અમે તેની વિગતો શેર કરી શકતા નથી.",
    },
    "error_fallback": {
        "en": "Something went wrong on our end. Please try again shortly, or we will connect you to our team.",
        "hi": "हमारी ओर से कुछ गड़बड़ी हुई है। कृपया थोड़ी देर बाद पुनः प्रयास करें, या हम आपको हमारी टीम से जोड़ देंगे।",
        "hinglish": "Kuch gadbad ho gayi hai hamari taraf se. Thodi der baad try karein, ya hum aapko team se connect kar denge.",
        "gu": "અમારી તરફથી કંઈક ગડબડ થઈ છે. કૃપા કરીને થોડી વાર પછી ફરી પ્રયાસ કરો, અથવા અમે તમને અમારી ટીમ સાથે જોડીશું.",
    },
}


def copy_for(key: str, language: str) -> str:
    entry = _COPY[key]
    return entry.get(language, entry[DEFAULT_LANGUAGE])
```

> **Note for the owner/client review pass:** this is deterministic *system* copy (confirm/
> cancel/not-found/refusal/error), distinct from the FAQ/policy content blocked on client
> Q14. Recommend a quick client sign-off on tone/wording before Phase 4 wires this in, same
> as the approved WhatsApp templates were reviewed — flag as a copy-paste-ready question if
> the owner wants that gate.

- [ ] **Step 4: Run to verify PASS** — tests green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: deterministic multilingual reply copy (en/hi/hinglish/gu)"`

---

### Task 7: Webhook router (GET verify + POST receive) + wiring

**Files:**
- Create: `backend/app/channels/whatsapp.py`
- Modify: `backend/app/deps.py` (add `messages: MessageStore` to `Container`),
  `backend/app/main.py` (include router)
- Test: `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: `verify_meta_hmac` (Task 1), `load_whatsapp_config` (Task 2), `extract_event`
  (Task 3), `MessageStore` (Task 4), `get_container()`.
- Produces: `router` with `GET /webhook/whatsapp` (hub challenge-response, ASCII-safe
  constant-time verify-token compare) and `POST /webhook/whatsapp` → 403 bad HMAC or
  unconfigured; 200 `{"ok": true, "ignored": true}` for status callbacks / unparseable /
  foreign `phone_number_id`; 200 `{"ok": true, "duplicate": true}` on replay; 200
  `{"ok": true, "duplicate": false, "event_type": <ClassName>}` on a fresh recognized event
  (no further action — Phase 4/5 attach the router here).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_whatsapp_webhook.py`:
```python
import hashlib
import hmac as hmac_lib
import json

import httpx
import pytest

from app.deps import get_container, reset_container

SECRET = "app-secret-webhook"
VERIFY_TOKEN = "verify-me"
PHONE_NUMBER_ID = "1298805403309058"


@pytest.fixture(autouse=True)
async def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    await c.config.set_secret("whatsapp:access_token", "tok")
    await c.config.set_secret("whatsapp:app_secret", SECRET)
    await c.config.set_secret("whatsapp:verify_token", VERIFY_TOKEN)
    await c.config.set_plain("whatsapp:phone_number_id", PHONE_NUMBER_ID)
    await c.config.set_plain("whatsapp:waba_id", "2454816495000045")
    await c.config.set_plain("whatsapp:api_version", "v23.0")
    yield
    reset_container()


def envelope(message: dict, phone_number_id: str = PHONE_NUMBER_ID) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [message],
                },
            }],
        }],
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac_lib.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def get(path: str, params: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def post(body: bytes, headers: dict) -> httpx.Response:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook/whatsapp", content=body, headers=headers)


async def test_get_verify_success() -> None:
    resp = await get(
        "/webhook/whatsapp",
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "xyz123"},
    )
    assert resp.status_code == 200
    assert resp.text == "xyz123"


async def test_get_verify_wrong_token_403() -> None:
    resp = await get(
        "/webhook/whatsapp",
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "xyz123"},
    )
    assert resp.status_code == 403


async def test_post_bad_hmac_403() -> None:
    body = json.dumps(envelope({"from": "919999999999", "id": "wamid.1", "type": "text", "text": {"body": "hi"}})).encode()
    resp = await post(body, {"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert resp.status_code == 403


async def test_post_new_text_event_acknowledged() -> None:
    body = json.dumps(envelope({"from": "919999999999", "id": "wamid.1", "timestamp": "1", "type": "text", "text": {"body": "hi"}})).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "duplicate": False, "event_type": "InboundText"}


async def test_post_replay_is_duplicate() -> None:
    body = json.dumps(envelope({"from": "919999999999", "id": "wamid.2", "timestamp": "1", "type": "text", "text": {"body": "hi"}})).encode()
    headers = {"X-Hub-Signature-256": sign(body)}
    await post(body, headers)
    resp = await post(body, headers)
    assert resp.json() == {"ok": True, "duplicate": True}


async def test_post_status_callback_ignored() -> None:
    body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.s1"}]}}]}]}).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_foreign_phone_number_id_ignored() -> None:
    body = json.dumps(envelope(
        {"from": "919999999999", "id": "wamid.3", "timestamp": "1", "type": "text", "text": {"body": "hi"}},
        phone_number_id="9999999999999",
    )).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"ok": True, "ignored": True}


async def test_post_button_tap_event_type() -> None:
    body = json.dumps(envelope({
        "from": "919999999999", "id": "wamid.4", "timestamp": "1", "type": "button",
        "button": {"text": "Confirm Order", "payload": "order:confirm:gid://1"},
    })).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"ok": True, "duplicate": False, "event_type": "InboundButton"}


async def test_post_garbage_body_ignored() -> None:
    body = b"not-json"
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": True}
```

- [ ] **Step 2: Run to verify FAIL** — 404 (router not mounted) / `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/channels/whatsapp.py`:
```python
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_inbound import extract_event
from app.channels.whatsapp_signature import verify_meta_hmac
from app.deps import get_container

router = APIRouter()

MAX_WEBHOOK_BODY_BYTES = 1_048_576


def _ascii_compare(expected: str, provided: str) -> bool:
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided_bytes)


def _incoming_phone_number_id(payload: dict[str, Any]) -> str | None:
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        phone_id = (value.get("metadata") or {}).get("phone_number_id")
    except (KeyError, IndexError, TypeError):
        return None
    return phone_id if isinstance(phone_id, str) else None


@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request) -> Response:
    cfg = await load_whatsapp_config(get_container().config)
    if cfg is None:
        return PlainTextResponse("forbidden", status_code=403)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and challenge is not None and _ascii_compare(cfg.verify_token, token):
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request) -> Response:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > MAX_WEBHOOK_BODY_BYTES
        except ValueError:
            too_big = False
        if too_big:
            return PlainTextResponse("payload too large", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BODY_BYTES:
        return PlainTextResponse("payload too large", status_code=413)

    c = get_container()
    cfg = await load_whatsapp_config(c.config)
    if cfg is None or not verify_meta_hmac(
        raw, request.headers.get("X-Hub-Signature-256"), cfg.app_secret
    ):
        return PlainTextResponse("forbidden", status_code=403)

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True, "ignored": True})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "ignored": True})

    # Defensive check mirroring the Shopify shop-domain guard: ignore deliveries for a
    # phone number we are not configured to serve.
    incoming_phone_id = _incoming_phone_number_id(payload)
    if incoming_phone_id is not None and incoming_phone_id != cfg.phone_number_id:
        return JSONResponse({"ok": True, "ignored": True})

    event = extract_event(payload)
    if event is None:
        return JSONResponse({"ok": True, "ignored": True})

    is_new = await c.messages.record_if_new(event.message_id)
    if not is_new:
        return JSONResponse({"ok": True, "duplicate": True})

    # Routing a fresh event to the deterministic button dispatcher (Phase 5) and the
    # conversation engine / order_resolver (Phase 4) attaches here. Phase 3 is the pipe only.
    return JSONResponse({"ok": True, "duplicate": False, "event_type": type(event).__name__})
```

Modify `backend/app/deps.py`:
```python
from app.store.base import ConfigRepo, IngestStore, MessageStore
from app.store.memory import InMemoryConfigRepo, InMemoryIngestStore, InMemoryMessageStore
from app.store.postgres import PostgresConfigRepo, PostgresIngestStore, PostgresMessageStore


@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient
    ingest: IngestStore
    messages: MessageStore


def get_container() -> Container:
    global _container
    if _container is None:
        settings = Settings()  # type: ignore[call-arg]
        vault = SecretVault(settings.app_master_key)
        if settings.database_url:
            pool = LazyPool(settings.database_url)
            config_repo: ConfigRepo = PostgresConfigRepo(pool)
            ingest: IngestStore = PostgresIngestStore(pool)
            messages: MessageStore = PostgresMessageStore(pool)
        else:
            config_repo = InMemoryConfigRepo()
            ingest = InMemoryIngestStore()
            messages = InMemoryMessageStore()
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient(follow_redirects=False)
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(
            settings, vault, config_repo, config, http, tokens, shopify, ingest, messages
        )
    return _container
```
(`PostgresMessageStore` is built in Task 8 — this task can stub it as a forward reference
or Tasks 7/8 can be done in the order given since `postgres.py` already exists and is only
being extended, not created.)

Modify `backend/app/main.py`: add `from app.channels.whatsapp import router as whatsapp_router`
and `app.include_router(whatsapp_router)`.

- [ ] **Step 4: Run to verify PASS** — full suite green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Meta webhook GET verify + POST receive (hex HMAC, dedupe, typed dispatch stub)"`

---

### Task 8: Postgres MessageStore (gated tests) + container switch

**Files:**
- Modify: `backend/app/store/postgres.py`
- Test: `backend/tests/test_postgres_message_store.py`, append to `backend/tests/test_health.py`

**Interfaces:**
- Produces: `PostgresMessageStore(pool: LazyPool)` implementing `MessageStore` —
  `INSERT INTO processed_messages (message_id) VALUES ($1) ON CONFLICT DO NOTHING`,
  rowcount → `record_if_new` result (same command-tag-suffix technique as `PostgresIngestStore`).

- [ ] **Step 1: Write the tests (gated — SKIP without `TEST_DATABASE_URL`)**

`backend/tests/test_postgres_message_store.py`:
```python
import os
import uuid

import pytest

from app.store.pg_factory import LazyPool
from app.store.postgres import PostgresMessageStore

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    p = LazyPool(DSN)
    yield p
    await p.close()


async def test_record_if_new_then_duplicate(pool: LazyPool) -> None:
    store = PostgresMessageStore(pool)
    message_id = f"wamid.{uuid.uuid4()}"
    assert await store.record_if_new(message_id) is True
    assert await store.record_if_new(message_id) is False
```

Append to `backend/tests/test_health.py`:
```python
def test_container_uses_postgres_message_store_when_database_url_set(
    master_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.store.postgres import PostgresMessageStore

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    reset_container()
    c = get_container()
    assert isinstance(c.messages, PostgresMessageStore)
    reset_container()
```

- [ ] **Step 2: Run to verify SKIP offline / FAIL on the health test** — Postgres test
  SKIPPED; health test fails (`AttributeError`/`ImportError`) since `PostgresMessageStore`
  doesn't exist yet.

- [ ] **Step 3: Implement**

Append to `backend/app/store/postgres.py`:
```python
class PostgresMessageStore:
    def __init__(self, pool: LazyPool) -> None:
        self._pool = pool

    async def record_if_new(self, message_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO processed_messages (message_id) VALUES ($1) ON CONFLICT DO NOTHING",
                message_id,
            )
        return not result.endswith("0")
```

(The `deps.py` wiring for this was already written in Task 7's `get_container()` change —
if Task 7 was implemented before this class existed, add the `PostgresMessageStore` import
and branch now.)

- [ ] **Step 4: Full verification sweep** — `python -m pytest -q` (all green, Postgres tests
  SKIP offline); `ruff check .`; `mypy app`; secrets grep EMPTY. If `TEST_DATABASE_URL` is
  available: `TEST_DATABASE_URL=... python -m pytest tests/test_postgres_message_store.py -v`
  → 1 passed.

- [ ] **Step 5: Commit** — `git commit -m "feat: Postgres MessageStore + container switch"`

---

### Task 9: Live send smoke script + registry updates

**Files:**
- Create: `backend/scripts/smoke_whatsapp.py`
- Modify: `docs/memory/component_registry.md`, `docs/memory/api_registry.md`

**Interfaces:**
- Produces: `python -m scripts.smoke_whatsapp --to <E.164>` sends one real `send_text`
  message via the configured WABA and prints `ok`/`status_code`/`wamid` only — **never** the
  destination number or response body (Phase 1 security learning: smoke scripts must not
  print PII).

- [ ] **Step 1: Implement**

`backend/scripts/smoke_whatsapp.py`:
```python
"""Send a real WhatsApp text message via send_text.
Run: python -m scripts.smoke_whatsapp --to <E.164>
Requires whatsapp:* config already seeded (access_token/app_secret/verify_token/
phone_number_id/waba_id/api_version) via the admin panel or a seed script.
"""

import argparse
import asyncio

import httpx

from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_sender import send_text
from app.deps import get_container


async def main(to: str) -> None:
    c = get_container()
    cfg = await load_whatsapp_config(c.config)
    if cfg is None:
        raise SystemExit("whatsapp config incomplete — see module docstring")
    async with httpx.AsyncClient() as http:
        result = await send_text(http, cfg, to, "Thetavas bot smoke test: live send check.")
    # Never print the destination number or response body -- status/wamid only.
    print(f"ok={result.ok} status_code={result.status_code} wamid={result.wamid}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="E.164 recipient, e.g. +917575072795")
    args = parser.parse_args()
    asyncio.run(main(args.to))
```

- [ ] **Step 2: Compliance grep** — run the CLAUDE.md secrets grep against every new file in
  `app/channels/` and `scripts/smoke_whatsapp.py`; must return EMPTY.

- [ ] **Step 3: Update registries** — add to `docs/memory/component_registry.md`:
  `whatsapp_signature.verify_meta_hmac`, `whatsapp_config` (`WhatsAppConfig` +
  `load_whatsapp_config`), `whatsapp_inbound` (`InboundText`/`InboundInteractive`/
  `InboundButton`/`extract_event`), `MessageStore` (+ both impls), `whatsapp_sender`
  (`SendResult`/`WhatsAppSendError`/`send_text`/`send_template`/`send_buttons`), `copy.py`
  (`copy_for`), `whatsapp.py` webhook router. Add to `docs/memory/api_registry.md`:
  `GET/POST /webhook/whatsapp`; external: Meta Graph API
  `POST /{api_version}/{phone_number_id}/messages`.

- [ ] **Step 4: Final full-project sweep** — `python -m pytest -q` (all green, Postgres tests
  skip offline); `ruff check .`; `mypy app`; secrets grep EMPTY across the whole `app/` tree.

- [ ] **Step 5: Commit** — `git commit -m "chore: WhatsApp smoke script + Phase 3 registry updates"`

---

## Self-Review (done at plan time)

- **Coverage:** F4/error_learnings ("button" ≠ "interactive") → Task 3 (`InboundButton` as a
  distinct variant, explicit test). Two-HMAC-scheme lesson → Task 1 (hex, prefix-checked,
  explicitly tested against Shopify's base64 encoding to catch a copy-paste mistake).
  Non-ASCII header TypeError lesson → Tasks 1 & 7 (ASCII-safe compare, explicit test).
  Type-coercion-on-signed-input lesson → Task 3 (`isinstance` guards, type-confusion test).
  Smoke-script-PII lesson (Phase 1 security review) → Task 9 (no destination number/body
  printed). Config-as-not-code (ADR-005) → Task 2 (no hardcoded `api_version` default).
- **Deliberately absent:** `core/order_resolver.py`, `core/engine.py`, `app/knowledge/`,
  `conversations`/`messages` table repo code, deterministic button→mutation dispatch, outbox
  drain integration. These are Phase 4 (conversation) and Phase 5 (push automation) per the
  2026-07-28 re-sequencing — building them now against a still-hypothetical engine interface
  would be exactly the kind of speculative half-finished work CLAUDE.md's coding-style rules
  warn against. The webhook's final response (`event_type` echo) is the seam Phase 4/5 attach
  to; no interface is invented here that they would have to conform to or rework.
- **Placeholders:** none — full code every step.
- **Type consistency:** `InboundEvent` union used identically in Tasks 3/7; `MessageStore`
  Protocol signature (`record_if_new(message_id: str) -> bool`) identical across Tasks 4/7/8;
  `WhatsAppConfig` fields identical across Tasks 2/5/7/9; `Container.messages` used by Task 7
  and asserted in Task 8.
- **Open item for the owner/client, not blocking this plan:** the `copy.py` wording (Task 6)
  is system copy the owner may want the client to sign off on before Phase 4 wires it into
  live conversations — flagged inline in Task 6, not gating this build.
