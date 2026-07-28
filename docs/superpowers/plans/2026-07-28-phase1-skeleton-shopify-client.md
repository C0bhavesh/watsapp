# Phase 1 — Backend Skeleton + Shopify Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deployable FastAPI skeleton with a production-grade Shopify layer: Fernet secret vault, DB-persistable TokenManager (ADR-003), and a GraphQL client exposing the five verified operations with structural mutation safety (ADR-004).

**Architecture:** Ports & adapters mirroring the cafe project: `app/config` (settings + vault + config service), `app/store` (repo Protocol + in-memory impl; Postgres arrives Phase 2), `app/shopify` (token manager + client + pure models). `core` layers arrive in later phases. All external I/O via httpx; tests use `httpx.MockTransport` — no live calls in the suite.

**Tech Stack:** Python 3.12 · FastAPI · Pydantic v2 + pydantic-settings · httpx · cryptography (Fernet) · pytest + pytest-asyncio (asyncio_mode=auto) · ruff · mypy. Deploy target: Vercel serverless (`@vercel/python`), region **bom1** (Mumbai — cafe latency lesson).

## Global Constraints

- Only `APP_MASTER_KEY` and `DATABASE_URL` may come from env in production posture; Shopify `client_id`/`client_secret` live Fernet-encrypted in the config store under keys `shopify:client_id` / `shopify:client_secret` (CLAUDE.md Critical Rule 1).
- Never log or echo plaintext secrets or `shpat_` tokens; error strings must not contain them.
- Shopify API version comes ONLY from `Settings.shopify_api_version` = `"2026-07"` — never hardcoded in URLs.
- Shop domain `thetavas.myshopify.com` from `Settings.shop_domain`.
- Mutating client methods accept ONLY `AuthorizedOrder` (ADR-004). No method that takes a bare gid may mutate.
- Order display numbers: `"tavas" + order_number`, no `#` (verified live). `normalize_order_name` must accept `tavas3733`, `#tavas3733`, `3733`, `#3733` → `tavas3733`.
- COD detection: tag `cod` (case-insensitive) OR any `paymentGatewayNames` entry containing `cash on delivery` (verified live: `["Cash on Delivery (COD)"]`).
- Phone extraction chain: `order.phone` → `shippingAddress.phone` → `billingAddress.phone` (verified live; matches WATI bridge).
- After edits run: `ruff check backend/` and `mypy app` (from `backend/`) — both clean; full `pytest` green.
- Secrets grep before every commit: `grep -rnE "shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}" backend/ docs/` → must be EMPTY.
- Commits: conventional messages, end body with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. NEVER `git push` (owner approval required; Vercel unconnected but rule stands).

## File Structure (end state of Phase 1)

```
backend/
  api/index.py               # Vercel ASGI entrypoint
  vercel.json                # @vercel/python, all routes → api/index.py, region bom1
  requirements.txt
  pyproject.toml             # ruff + mypy + pytest config
  app/__init__.py
  app/main.py                # FastAPI app + GET /health
  app/deps.py                # composition root (singleton container + reset for tests)
  app/config/__init__.py
  app/config/settings.py     # pydantic-settings
  app/config/crypto.py       # SecretVault (Fernet) + VaultError
  app/config/service.py      # ConfigService (plain + encrypted config access)
  app/store/__init__.py
  app/store/base.py          # ConfigRepo Protocol
  app/store/memory.py        # InMemoryConfigRepo
  app/shopify/__init__.py
  app/shopify/errors.py      # error taxonomy
  app/shopify/models.py      # Order, Money, AuthorizedOrder, CancelRequested, normalize_order_name
  app/shopify/token_manager.py
  app/shopify/client.py      # ShopifyClient (5 ops)
  scripts/__init__.py
  scripts/smoke_shopify.py   # dev-only live smoke (env-driven, read-only + bogus-id mutation checks)
  tests/__init__.py
  tests/conftest.py
  tests/test_sanity.py
  tests/test_settings.py
  tests/test_crypto.py
  tests/test_config_service.py
  tests/test_models.py
  tests/test_token_manager.py
  tests/test_client_graphql.py
  tests/test_client_reads.py
  tests/test_client_mutations.py
  tests/test_health.py
```

---

### Task 1: Project scaffold + tooling config

**Files:**
- Create: `backend/requirements.txt`, `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/app/config/__init__.py`, `backend/app/store/__init__.py`, `backend/app/shopify/__init__.py`, `backend/scripts/__init__.py`, `backend/tests/__init__.py`, `backend/tests/test_sanity.py`

**Interfaces:**
- Produces: an importable `app` package; `pytest`, `ruff`, `mypy` all runnable from `backend/`.

- [ ] **Step 1: Create the files**

`backend/requirements.txt`:
```
fastapi>=0.115
pydantic>=2.7
pydantic-settings>=2.3
httpx>=0.27
cryptography>=42
pytest>=8.2
pytest-asyncio>=0.23
ruff>=0.5
mypy>=1.10
```

`backend/pyproject.toml`:
```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["app"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

All `__init__.py` files: empty.

`backend/tests/test_sanity.py`:
```python
import app


def test_app_package_importable() -> None:
    assert app is not None
```

- [ ] **Step 2: Install deps and run the suite**

Run (from `backend/`): `python -m pip install -r requirements.txt` then `python -m pytest -q`
Expected: `1 passed`

- [ ] **Step 3: Run linters**

Run: `ruff check .` and `mypy app`
Expected: both clean (no files yet beyond empty inits).

- [ ] **Step 4: Commit**

```bash
git add backend/
git commit -m "feat: backend scaffold with ruff/mypy/pytest tooling"
```

---

### Task 2: Settings (pydantic-settings, fail-fast master key)

**Files:**
- Create: `backend/app/config/settings.py`
- Test: `backend/tests/test_settings.py`, `backend/tests/conftest.py`

**Interfaces:**
- Produces: `Settings` with fields `app_master_key: str` (required), `database_url: str = ""`, `shop_domain: str = "thetavas.myshopify.com"`, `shopify_api_version: str = "2026-07"`, `request_timeout_seconds: float = 20.0`, `app_env: str = "dev"`.
- Produces (conftest): `master_key` fixture (valid Fernet key str), `settings` fixture.

- [ ] **Step 1: Write the failing tests**

`backend/tests/conftest.py`:
```python
import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def master_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def settings(master_key: str):
    from app.config.settings import Settings

    return Settings(app_master_key=master_key)
```

`backend/tests/test_settings.py`:
```python
import pytest
from pydantic import ValidationError


def test_defaults(settings) -> None:
    assert settings.shop_domain == "thetavas.myshopify.com"
    assert settings.shopify_api_version == "2026-07"
    assert settings.database_url == ""
    assert settings.request_timeout_seconds == 20.0
    assert settings.app_env == "dev"


def test_missing_master_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import Settings

    monkeypatch.delenv("APP_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: app.config.settings`

- [ ] **Step 3: Implement**

`backend/app/config/settings.py`:
```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_master_key: str
    database_url: str = ""
    shop_domain: str = "thetavas.myshopify.com"
    shopify_api_version: str = "2026-07"
    request_timeout_seconds: float = 20.0
    app_env: str = "dev"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_settings.py -v` → PASS; `mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config/settings.py backend/tests/
git commit -m "feat: Settings with fail-fast APP_MASTER_KEY and 2026-07 API version pin"
```

---

### Task 3: SecretVault (Fernet)

**Files:**
- Create: `backend/app/config/crypto.py`
- Test: `backend/tests/test_crypto.py`

**Interfaces:**
- Produces: `SecretVault(master_key: str)` with `.encrypt(plaintext: str) -> str`, `.decrypt(token: str) -> str`; `VaultError(Exception)`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_crypto.py`:
```python
import pytest

from app.config.crypto import SecretVault, VaultError


def test_roundtrip(master_key: str) -> None:
    vault = SecretVault(master_key)
    token = vault.encrypt("shh-secret")
    assert token != "shh-secret"
    assert vault.decrypt(token) == "shh-secret"


def test_invalid_master_key_fails_fast() -> None:
    with pytest.raises(VaultError):
        SecretVault("not-a-fernet-key")


def test_decrypt_garbage_raises(master_key: str) -> None:
    vault = SecretVault(master_key)
    with pytest.raises(VaultError):
        vault.decrypt("gAAAAAgarbage")
```

- [ ] **Step 2: Run to verify FAIL** — `python -m pytest tests/test_crypto.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/config/crypto.py`:
```python
from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    """Raised when the vault cannot be constructed or a value cannot be decrypted."""


class SecretVault:
    def __init__(self, master_key: str) -> None:
        try:
            self._fernet = Fernet(master_key.encode())
        except (ValueError, TypeError) as exc:
            raise VaultError("APP_MASTER_KEY is not a valid Fernet key") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise VaultError("decryption failed") from exc
```

- [ ] **Step 4: Run to verify PASS** — tests green, `ruff check .`, `mypy app` clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Fernet SecretVault with fail-fast invalid-key handling"`

---

### Task 4: ConfigRepo Protocol + in-memory impl + ConfigService

**Files:**
- Create: `backend/app/store/base.py`, `backend/app/store/memory.py`, `backend/app/config/service.py`
- Test: `backend/tests/test_config_service.py`

**Interfaces:**
- Produces: `ConfigRepo` Protocol — `async get(key: str) -> str | None`, `async set(key: str, value: str) -> None`.
- Produces: `InMemoryConfigRepo()` implementing it.
- Produces: `ConfigService(repo: ConfigRepo, vault: SecretVault)` — `async get_plain/set_plain`, `async get_secret/set_secret` (encrypted at rest).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_config_service.py`:
```python
from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.store.memory import InMemoryConfigRepo


async def test_plain_roundtrip(master_key: str) -> None:
    svc = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    assert await svc.get_plain("missing") is None
    await svc.set_plain("shopify:api_version", "2026-07")
    assert await svc.get_plain("shopify:api_version") == "2026-07"


async def test_secret_is_encrypted_at_rest(master_key: str) -> None:
    repo = InMemoryConfigRepo()
    svc = ConfigService(repo, SecretVault(master_key))
    await svc.set_secret("shopify:client_secret", "shpss_dummy_value")
    raw = await repo.get("shopify:client_secret")
    assert raw is not None and "shpss_dummy_value" not in raw
    assert await svc.get_secret("shopify:client_secret") == "shpss_dummy_value"


async def test_get_secret_missing_returns_none(master_key: str) -> None:
    svc = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    assert await svc.get_secret("nope") is None
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/store/base.py`:
```python
from typing import Protocol


class ConfigRepo(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...
```

`backend/app/store/memory.py`:
```python
class InMemoryConfigRepo:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        self._data[key] = value
```

`backend/app/config/service.py`:
```python
from app.config.crypto import SecretVault
from app.store.base import ConfigRepo


class ConfigService:
    def __init__(self, repo: ConfigRepo, vault: SecretVault) -> None:
        self._repo = repo
        self._vault = vault

    async def get_plain(self, key: str) -> str | None:
        return await self._repo.get(key)

    async def set_plain(self, key: str, value: str) -> None:
        await self._repo.set(key, value)

    async def get_secret(self, key: str) -> str | None:
        raw = await self._repo.get(key)
        return None if raw is None else self._vault.decrypt(raw)

    async def set_secret(self, key: str, value: str) -> None:
        await self._repo.set(key, self._vault.encrypt(value))
```

- [ ] **Step 4: Run to verify PASS** — suite green, ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: ConfigRepo protocol, in-memory impl, ConfigService with encrypted secrets"`

---

### Task 5: Shopify models + pure derivations

**Files:**
- Create: `backend/app/shopify/models.py`, `backend/app/shopify/errors.py`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Produces (`models.py`): `Money(amount: str, currency: str)`; `Order` frozen dataclass with fields `gid, name, email, phone, shipping_phone, billing_phone, financial_status, fulfillment_status, cancelled_at, tags: tuple[str, ...], payment_gateway_names: tuple[str, ...], total: Money | None, customer_locale: str | None` and methods `best_phone() -> str | None`, `is_cod() -> bool`, `is_cancelled() -> bool`; `AuthorizedOrder(order: Order, verified_phone: str)` frozen; `CancelRequested(job_id: str | None)` frozen; `normalize_order_name(raw: str, prefix: str = "tavas") -> str`.
- Produces (`errors.py`): `ShopifyError(Exception)` base; subclasses `ShopifyAuthError`, `ShopifyThrottled`, `ShopifyUnavailable`, `TokenGrantError`; `ShopifyGraphQLError(ShopifyError)` with `.messages: list[str]`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_models.py`:
```python
from app.shopify.models import Money, Order, normalize_order_name


def make_order(**overrides) -> Order:
    base = dict(
        gid="gid://shopify/Order/1",
        name="tavas3733",
        email="c@example.com",
        phone=None,
        shipping_phone=None,
        billing_phone=None,
        financial_status="PENDING",
        fulfillment_status="UNFULFILLED",
        cancelled_at=None,
        tags=(),
        payment_gateway_names=(),
        total=Money("949.0", "INR"),
        customer_locale="en",
    )
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


def test_best_phone_chain_order_first() -> None:
    o = make_order(phone="+911", shipping_phone="+912", billing_phone="+913")
    assert o.best_phone() == "+911"


def test_best_phone_falls_back_shipping_then_billing() -> None:
    assert make_order(shipping_phone="+912", billing_phone="+913").best_phone() == "+912"
    assert make_order(billing_phone="+913").best_phone() == "+913"
    assert make_order().best_phone() is None


def test_is_cod_via_gateway_and_tag() -> None:
    assert make_order(payment_gateway_names=("Cash on Delivery (COD)",)).is_cod()
    assert make_order(tags=("COD", "Shopflo")).is_cod()
    assert not make_order(payment_gateway_names=("Razorpay",), tags=("online",)).is_cod()


def test_is_cancelled() -> None:
    assert make_order(cancelled_at="2026-07-28T00:00:00Z").is_cancelled()
    assert not make_order().is_cancelled()


def test_normalize_order_name_variants() -> None:
    assert normalize_order_name("tavas3733") == "tavas3733"
    assert normalize_order_name("#tavas3733") == "tavas3733"
    assert normalize_order_name("3733") == "tavas3733"
    assert normalize_order_name("#3733") == "tavas3733"
    assert normalize_order_name("  TAVAS3733 ") == "tavas3733"
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/shopify/errors.py`:
```python
class ShopifyError(Exception):
    """Base for all Shopify-layer failures."""


class ShopifyAuthError(ShopifyError):
    """Token rejected even after a forced refresh."""


class ShopifyThrottled(ShopifyError):
    """GraphQL cost throttle hit — retryable."""


class ShopifyUnavailable(ShopifyError):
    """Network-level failure talking to Shopify — retryable."""


class TokenGrantError(ShopifyError):
    """client_credentials grant failed."""


class ShopifyGraphQLError(ShopifyError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages
```

`backend/app/shopify/models.py`:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    amount: str
    currency: str


@dataclass(frozen=True)
class Order:
    gid: str
    name: str
    email: str | None
    phone: str | None
    shipping_phone: str | None
    billing_phone: str | None
    financial_status: str | None
    fulfillment_status: str | None
    cancelled_at: str | None
    tags: tuple[str, ...]
    payment_gateway_names: tuple[str, ...]
    total: Money | None
    customer_locale: str | None

    def best_phone(self) -> str | None:
        return self.phone or self.shipping_phone or self.billing_phone

    def is_cod(self) -> bool:
        if any("cash on delivery" in g.lower() for g in self.payment_gateway_names):
            return True
        return any(t.strip().lower() == "cod" for t in self.tags)

    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


@dataclass(frozen=True)
class AuthorizedOrder:
    """Only core.order_resolver may construct this in production code (ADR-004)."""

    order: Order
    verified_phone: str


@dataclass(frozen=True)
class CancelRequested:
    job_id: str | None


def normalize_order_name(raw: str, prefix: str = "tavas") -> str:
    name = raw.strip().lstrip("#").lower()
    if name.isdigit():
        return f"{prefix}{name}"
    return name
```

- [ ] **Step 4: Run to verify PASS** — suite green, ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Shopify domain models, error taxonomy, order-name normalization"`

---

### Task 6: TokenManager (ADR-003)

**Files:**
- Create: `backend/app/shopify/token_manager.py`
- Test: `backend/tests/test_token_manager.py`

**Interfaces:**
- Consumes: `ConfigService` (Task 4), `Settings` (Task 2), `TokenGrantError` (Task 5).
- Produces: `TokenManager(http: httpx.AsyncClient, config: ConfigService, settings: Settings, now: Callable[[], float] = time.time)` with `async get_token() -> str`, `async force_refresh() -> str`. Config keys: `shopify:client_id` / `shopify:client_secret` (secrets), `shopify:access_token` (secret), `shopify:token_expires_at` (plain, unix float str). Refresh margin constant `REFRESH_MARGIN_SECONDS = 3600`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_token_manager.py`:
```python
import asyncio
import json

import httpx
import pytest

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.shopify.errors import TokenGrantError
from app.shopify.token_manager import TokenManager


def make_manager(settings, master_key, responder, now=lambda: 1_000_000.0):
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(__import__("app.store.memory", fromlist=["m"]).InMemoryConfigRepo(),
                           SecretVault(master_key))
    return TokenManager(http, config, settings, now=now), config, calls


def ok_grant(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "shpat_test_token", "expires_in": 86399})


async def seed(config: ConfigService) -> None:
    await config.set_secret("shopify:client_id", "cid")
    await config.set_secret("shopify:client_secret", "csec")


async def test_grant_fetches_stores_and_caches(settings, master_key) -> None:
    mgr, config, calls = make_manager(settings, master_key, ok_grant)
    await seed(config)
    token = await mgr.get_token()
    assert token == "shpat_test_token"
    assert len(calls) == 1
    body = calls[0].content.decode()
    assert "grant_type=client_credentials" in body and "cid" in body
    assert await config.get_secret("shopify:access_token") == "shpat_test_token"
    # second call: cached, no new HTTP
    assert await mgr.get_token() == "shpat_test_token"
    assert len(calls) == 1


async def test_expired_store_token_triggers_refresh(settings, master_key) -> None:
    now = {"t": 1_000_000.0}
    mgr, config, calls = make_manager(settings, master_key, ok_grant, now=lambda: now["t"])
    await seed(config)
    await mgr.get_token()
    now["t"] += 86399 - 100  # inside the 1h refresh margin
    await mgr.get_token()
    assert len(calls) == 2


async def test_single_flight_concurrent_calls_one_grant(settings, master_key) -> None:
    mgr, config, calls = make_manager(settings, master_key, ok_grant)
    await seed(config)
    await asyncio.gather(mgr.get_token(), mgr.get_token(), mgr.get_token())
    assert len(calls) == 1


async def test_grant_failure_raises_without_leaking_secret(settings, master_key) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": "invalid client"})

    mgr, config, _ = make_manager(settings, master_key, bad)
    await seed(config)
    with pytest.raises(TokenGrantError) as exc_info:
        await mgr.get_token()
    assert "csec" not in str(exc_info.value)


async def test_missing_credentials_raise(settings, master_key) -> None:
    mgr, _config, _ = make_manager(settings, master_key, ok_grant)
    with pytest.raises(TokenGrantError):
        await mgr.get_token()
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/shopify/token_manager.py`:
```python
import asyncio
import time
from collections.abc import Callable

import httpx

from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.errors import TokenGrantError

TOKEN_KEY = "shopify:access_token"
EXPIRES_KEY = "shopify:token_expires_at"
CLIENT_ID_KEY = "shopify:client_id"
CLIENT_SECRET_KEY = "shopify:client_secret"
REFRESH_MARGIN_SECONDS = 3600.0


class TokenManager:
    def __init__(
        self,
        http: httpx.AsyncClient,
        config: ConfigService,
        settings: Settings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._config = config
        self._settings = settings
        self._now = now
        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        self._cached_expires_at = 0.0

    def _fresh(self, expires_at: float) -> bool:
        return expires_at - self._now() > REFRESH_MARGIN_SECONDS

    async def get_token(self) -> str:
        if self._cached_token is not None and self._fresh(self._cached_expires_at):
            return self._cached_token
        async with self._lock:
            if self._cached_token is not None and self._fresh(self._cached_expires_at):
                return self._cached_token
            stored = await self._config.get_secret(TOKEN_KEY)
            expires_raw = await self._config.get_plain(EXPIRES_KEY)
            if stored is not None and expires_raw is not None and self._fresh(float(expires_raw)):
                self._cached_token = stored
                self._cached_expires_at = float(expires_raw)
                return stored
            return await self._grant()

    async def force_refresh(self) -> str:
        async with self._lock:
            return await self._grant()

    async def _grant(self) -> str:
        client_id = await self._config.get_secret(CLIENT_ID_KEY)
        client_secret = await self._config.get_secret(CLIENT_SECRET_KEY)
        if not client_id or not client_secret:
            raise TokenGrantError("Shopify client credentials are not configured")
        url = f"https://{self._settings.shop_domain}/admin/oauth/access_token"
        try:
            resp = await self._http.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TokenGrantError("token endpoint unreachable") from exc
        if resp.status_code != 200:
            raise TokenGrantError(f"token grant rejected (HTTP {resp.status_code})")
        payload = resp.json()
        token = str(payload["access_token"])
        expires_at = self._now() + float(payload.get("expires_in", 86399))
        await self._config.set_secret(TOKEN_KEY, token)
        await self._config.set_plain(EXPIRES_KEY, str(expires_at))
        self._cached_token = token
        self._cached_expires_at = expires_at
        return token
```

- [ ] **Step 4: Run to verify PASS** — `python -m pytest tests/test_token_manager.py -v` green; full suite green; ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: TokenManager with store persistence, refresh margin, single-flight (ADR-003)"`

---

### Task 7: ShopifyClient GraphQL transport

**Files:**
- Create: `backend/app/shopify/client.py` (transport part)
- Test: `backend/tests/test_client_graphql.py`

**Interfaces:**
- Consumes: `TokenManager` (Task 6), errors (Task 5), `Settings`.
- Produces: `ShopifyClient(http, tokens, settings)` with `async _graphql(query: str, variables: dict | None = None) -> dict` — adds `X-Shopify-Access-Token`, URL `https://{shop_domain}/admin/api/{shopify_api_version}/graphql.json`; on HTTP 401 → `force_refresh()` + retry ONCE then `ShopifyAuthError`; `httpx.HTTPError` → `ShopifyUnavailable`; GraphQL `errors` with `THROTTLED` → `ShopifyThrottled`; other `errors` with `data` null → `ShopifyGraphQLError`; partial data + errors → return data (denied fields handled by callers).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_client_graphql.py`:
```python
import httpx
import pytest

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.shopify.client import ShopifyClient
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.token_manager import TokenManager
from app.store.memory import InMemoryConfigRepo


def make_client(settings, master_key, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    tokens = TokenManager(http, config, settings)
    return ShopifyClient(http, tokens, settings), config


async def seed(config) -> None:
    await config.set_secret("shopify:client_id", "cid")
    await config.set_secret("shopify:client_secret", "csec")


def grant_or(payload_fn):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "shpat_t1", "expires_in": 86399})
        return payload_fn(request)

    return handler


async def test_graphql_sends_token_and_version(settings, master_key) -> None:
    seen: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("X-Shopify-Access-Token")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"ok": True}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    data = await client._graphql("{ shop { name } }")
    assert data == {"ok": True}
    assert seen["header"] == "shpat_t1"
    assert "/admin/api/2026-07/graphql.json" in seen["path"]


async def test_http_401_refreshes_once_then_raises(settings, master_key) -> None:
    count = {"gql": 0, "grants": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            count["grants"] += 1
            return httpx.Response(200, json={"access_token": f"shpat_{count['grants']}", "expires_in": 86399})
        count["gql"] += 1
        return httpx.Response(401, json={"errors": "unauthorized"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await seed(config)
    client = ShopifyClient(http, TokenManager(http, config, settings), settings)
    with pytest.raises(ShopifyAuthError):
        await client._graphql("{ shop { name } }")
    assert count["gql"] == 2 and count["grants"] == 2


async def test_throttled_maps_to_typed_error(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "data": None,
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyThrottled):
        await client._graphql("{ shop { name } }")


async def test_errors_with_null_data_raise_graphql_error(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Access denied for customers field."}], "data": None})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError) as e:
        await client._graphql("{ customers { id } }")
    assert "Access denied" in e.value.messages[0]


async def test_partial_data_with_errors_returns_data(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Access denied for customer field."}],
            "data": {"orders": {"edges": []}},
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client._graphql("{ orders { edges } }") == {"orders": {"edges": []}}


async def test_network_error_maps_to_unavailable(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyUnavailable):
        await client._graphql("{ shop { name } }")
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`backend/app/shopify/client.py`:
```python
from typing import Any

import httpx

from app.config.settings import Settings
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.shopify.token_manager import TokenManager


class ShopifyClient:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, settings: Settings) -> None:
        self._http = http
        self._tokens = tokens
        self._settings = settings

    @property
    def _url(self) -> str:
        s = self._settings
        return f"https://{s.shop_domain}/admin/api/{s.shopify_api_version}/graphql.json"

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._tokens.get_token()
            try:
                resp = await self._http.post(
                    self._url,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": token},
                    timeout=self._settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise ShopifyUnavailable("network failure talking to Shopify") from exc
            if resp.status_code == 401:
                if attempt == 1:
                    await self._tokens.force_refresh()
                    continue
                raise ShopifyAuthError("Shopify rejected the token after refresh")
            payload = resp.json()
            errors = payload.get("errors")
            data = payload.get("data")
            if errors:
                messages = [str(e.get("message", "")) for e in errors]
                codes = {str(e.get("extensions", {}).get("code", "")) for e in errors}
                if "THROTTLED" in codes:
                    raise ShopifyThrottled("; ".join(messages))
                if data is None:
                    raise ShopifyGraphQLError(messages)
            if data is None:
                raise ShopifyGraphQLError(["empty response data"])
            return dict(data)
        raise ShopifyAuthError("unreachable")  # pragma: no cover
```

- [ ] **Step 4: Run to verify PASS** — suite green, ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: ShopifyClient GraphQL transport with 401-refresh, throttle and error taxonomy"`

---

### Task 8: Read operations (get_order, find_order_by_name, find_customer_orders_by_phone)

**Files:**
- Modify: `backend/app/shopify/client.py` (append methods + module helpers)
- Test: `backend/tests/test_client_reads.py`

**Interfaces:**
- Produces: `ORDER_FIELDS` fragment str; `_order_from_node(node: dict) -> Order`; `async get_order(gid: str) -> Order | None`; `async find_order_by_name(raw_name: str) -> Order | None`; `async find_customer_orders_by_phone(phone_e164: str) -> list[Order]` (returns `[]` on access-denied — `read_customers` scope may be absent).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_client_reads.py`:
```python
import json

import httpx

from tests.test_client_graphql import grant_or, make_client, seed

ORDER_NODE = {
    "id": "gid://shopify/Order/12187547894128",
    "name": "tavas3733",
    "email": "c@example.com",
    "phone": "+919999999999",
    "tags": ["COD", "COD pending"],
    "paymentGatewayNames": ["Cash on Delivery (COD)"],
    "displayFinancialStatus": "PENDING",
    "displayFulfillmentStatus": "UNFULFILLED",
    "cancelledAt": None,
    "customerLocale": "en-IN",
    "totalPriceSet": {"shopMoney": {"amount": "949.0", "currencyCode": "INR"}},
    "shippingAddress": {"phone": "+918888888888"},
    "billingAddress": {"phone": None},
}


async def test_get_order_parses_full_node(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": ORDER_NODE}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.get_order("gid://shopify/Order/12187547894128")
    assert order is not None
    assert order.name == "tavas3733"
    assert order.is_cod()
    assert order.best_phone() == "+919999999999"
    assert order.customer_locale == "en-IN"
    assert order.total is not None and order.total.currency == "INR"


async def test_get_order_missing_returns_none(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"node": None}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.get_order("gid://shopify/Order/1") is None


async def test_find_order_by_name_normalizes_and_queries(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": ORDER_NODE}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    order = await client.find_order_by_name("#3733")
    assert order is not None and order.gid.endswith("12187547894128")
    assert captured["variables"]["q"] == "name:tavas3733"


async def test_find_order_by_name_none_found(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {"edges": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_order_by_name("9999") is None


async def test_customer_search_access_denied_returns_empty(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "errors": [{"message": "Access denied for customers field."}], "data": None,
        })

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    assert await client.find_customer_orders_by_phone("+919999999999") == []


async def test_customer_search_two_step(settings, master_key) -> None:
    step = {"n": 0}

    def gql(request: httpx.Request) -> httpx.Response:
        step["n"] += 1
        if step["n"] == 1:
            return httpx.Response(200, json={"data": {"customers": {"edges": [
                {"node": {"id": "gid://shopify/Customer/77"}}
            ]}}})
        return httpx.Response(200, json={"data": {"orders": {"edges": [{"node": ORDER_NODE}]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    orders = await client.find_customer_orders_by_phone("+919999999999")
    assert len(orders) == 1 and orders[0].name == "tavas3733"
```

- [ ] **Step 2: Run to verify FAIL** — `AttributeError: get_order`

- [ ] **Step 3: Implement — append to `backend/app/shopify/client.py`**

Add imports at top: `from app.shopify.models import Money, Order, normalize_order_name` and append:

```python
ORDER_FIELDS = (
    "id name email phone tags paymentGatewayNames displayFinancialStatus "
    "displayFulfillmentStatus cancelledAt customerLocale "
    "totalPriceSet { shopMoney { amount currencyCode } } "
    "shippingAddress { phone } billingAddress { phone }"
)


def _order_from_node(node: dict[str, Any]) -> Order:
    total_node = (node.get("totalPriceSet") or {}).get("shopMoney")
    return Order(
        gid=str(node["id"]),
        name=str(node["name"]),
        email=node.get("email"),
        phone=node.get("phone"),
        shipping_phone=(node.get("shippingAddress") or {}).get("phone"),
        billing_phone=(node.get("billingAddress") or {}).get("phone"),
        financial_status=node.get("displayFinancialStatus"),
        fulfillment_status=node.get("displayFulfillmentStatus"),
        cancelled_at=node.get("cancelledAt"),
        tags=tuple(node.get("tags") or ()),
        payment_gateway_names=tuple(node.get("paymentGatewayNames") or ()),
        total=Money(str(total_node["amount"]), str(total_node["currencyCode"])) if total_node else None,
        customer_locale=node.get("customerLocale"),
    )
```

And methods on `ShopifyClient`:

```python
    async def get_order(self, gid: str) -> Order | None:
        query = f"query($id: ID!) {{ node(id: $id) {{ ... on Order {{ {ORDER_FIELDS} }} }} }}"
        data = await self._graphql(query, {"id": gid})
        node = data.get("node")
        return _order_from_node(node) if node else None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        name = normalize_order_name(raw_name)
        query = (
            f"query($q: String!) {{ orders(first: 1, query: $q) "
            f"{{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}"
        )
        data = await self._graphql(query, {"q": f"name:{name}"})
        edges = (data.get("orders") or {}).get("edges") or []
        return _order_from_node(edges[0]["node"]) if edges else None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        try:
            cust = await self._graphql(
                'query($q: String!) { customers(first: 1, query: $q) { edges { node { id } } } }',
                {"q": f"phone:{phone_e164}"},
            )
        except ShopifyGraphQLError as exc:
            if any("access denied" in m.lower() for m in exc.messages):
                return []
            raise
        edges = (cust.get("customers") or {}).get("edges") or []
        if not edges:
            return []
        customer_id = str(edges[0]["node"]["id"]).rsplit("/", 1)[-1]
        data = await self._graphql(
            f"query($q: String!) {{ orders(first: 10, query: $q, sortKey: CREATED_AT, reverse: true) "
            f"{{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }}",
            {"q": f"customer_id:{customer_id}"},
        )
        return [_order_from_node(e["node"]) for e in (data.get("orders") or {}).get("edges") or []]
```

- [ ] **Step 4: Run to verify PASS** — suite green, ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: Shopify read ops — get_order, find_order_by_name, customer-phone fallback"`

---

### Task 9: Mutations (add_tags, cancel_order — ADR-004)

**Files:**
- Modify: `backend/app/shopify/client.py`
- Test: `backend/tests/test_client_mutations.py`

**Interfaces:**
- Consumes: `AuthorizedOrder`, `CancelRequested` (Task 5).
- Produces: `async add_tags(auth: AuthorizedOrder, tags: Sequence[str]) -> None` (raises `ShopifyGraphQLError` on userErrors); `async cancel_order(auth: AuthorizedOrder, *, reason: str = "CUSTOMER", restock: bool = True) -> CancelRequested` (reads `orderCancelUserErrors`, returns job id; caller does two-phase tagging per ADR-004 — this method does NOT tag).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_client_mutations.py`:
```python
import json

import httpx
import pytest

from app.shopify.errors import ShopifyGraphQLError
from app.shopify.models import AuthorizedOrder, CancelRequested
from tests.test_client_graphql import grant_or, make_client, seed
from tests.test_models import make_order

AUTH = AuthorizedOrder(order=make_order(), verified_phone="+919999999999")


async def test_add_tags_sends_gid_and_tags(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"tagsAdd": {"userErrors": []}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    await client.add_tags(AUTH, ["confirmed"])
    assert captured["variables"] == {"id": AUTH.order.gid, "tags": ["confirmed"]}


async def test_add_tags_user_errors_raise(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"tagsAdd": {"userErrors": [
            {"message": "Order does not exist"}
        ]}}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError):
        await client.add_tags(AUTH, ["confirmed"])


async def test_cancel_order_returns_job_and_reads_typed_errors(settings, master_key) -> None:
    captured: dict = {}

    def gql(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"orderCancel": {
            "job": {"id": "gid://shopify/Job/9"}, "orderCancelUserErrors": [],
        }}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    result = await client.cancel_order(AUTH)
    assert result == CancelRequested(job_id="gid://shopify/Job/9")
    assert captured["variables"]["reason"] == "CUSTOMER"
    assert captured["variables"]["restock"] is True


async def test_cancel_order_user_errors_raise(settings, master_key) -> None:
    def gql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orderCancel": {
            "job": None,
            "orderCancelUserErrors": [{"message": "Order already cancelled"}],
        }}})

    client, config = make_client(settings, master_key, grant_or(gql))
    await seed(config)
    with pytest.raises(ShopifyGraphQLError):
        await client.cancel_order(AUTH)
```

- [ ] **Step 2: Run to verify FAIL** — `AttributeError: add_tags`

- [ ] **Step 3: Implement — append to `ShopifyClient`** (add `from collections.abc import Sequence` and `from app.shopify.models import AuthorizedOrder, CancelRequested` to imports):

```python
    async def add_tags(self, auth: AuthorizedOrder, tags: Sequence[str]) -> None:
        data = await self._graphql(
            "mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) "
            "{ userErrors { message } } }",
            {"id": auth.order.gid, "tags": list(tags)},
        )
        errors = (data.get("tagsAdd") or {}).get("userErrors") or []
        if errors:
            raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])

    async def cancel_order(
        self, auth: AuthorizedOrder, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> CancelRequested:
        data = await self._graphql(
            "mutation($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!) "
            "{ orderCancel(orderId: $orderId, reason: $reason, restock: $restock) "
            "{ job { id } orderCancelUserErrors { message } } }",
            {"orderId": auth.order.gid, "reason": reason, "restock": restock},
        )
        node = data.get("orderCancel") or {}
        errors = node.get("orderCancelUserErrors") or []
        if errors:
            raise ShopifyGraphQLError([str(e.get("message", "")) for e in errors])
        job = node.get("job") or {}
        return CancelRequested(job_id=job.get("id"))
```

- [ ] **Step 4: Run to verify PASS** — suite green, ruff + mypy clean (mypy confirms mutations only accept `AuthorizedOrder`).

- [ ] **Step 5: Commit** — `git commit -m "feat: mutations add_tags/cancel_order gated on AuthorizedOrder (ADR-004)"`

---

### Task 10: Composition root, FastAPI app, Vercel entrypoint

**Files:**
- Create: `backend/app/deps.py`, `backend/app/main.py`, `backend/api/index.py`, `backend/vercel.json`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `Container` dataclass (settings, vault, config_repo, config, http, tokens, shopify); `get_container() -> Container` (module singleton); `reset_container() -> None` (tests); FastAPI `app` with `GET /health` → `{"status": "ok", "service": "thetavas-order-bot"}`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_health.py`:
```python
import httpx
import pytest

import app.deps as deps_module
from app.deps import get_container, reset_container


@pytest.fixture(autouse=True)
def _fresh_container(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    yield
    reset_container()


async def test_health_returns_ok() -> None:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "thetavas-order-bot"}


def test_container_is_singleton() -> None:
    assert get_container() is get_container()


def test_container_wires_shopify_layer() -> None:
    c = get_container()
    assert c.shopify is not None and c.tokens is not None
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError: app.deps`

- [ ] **Step 3: Implement**

`backend/app/deps.py`:
```python
from dataclasses import dataclass

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.client import ShopifyClient
from app.shopify.token_manager import TokenManager
from app.store.base import ConfigRepo
from app.store.memory import InMemoryConfigRepo


@dataclass
class Container:
    settings: Settings
    vault: SecretVault
    config_repo: ConfigRepo
    config: ConfigService
    http: httpx.AsyncClient
    tokens: TokenManager
    shopify: ShopifyClient


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        settings = Settings()
        vault = SecretVault(settings.app_master_key)
        config_repo: ConfigRepo = InMemoryConfigRepo()  # Phase 2: Postgres when database_url set
        config = ConfigService(config_repo, vault)
        http = httpx.AsyncClient()
        tokens = TokenManager(http, config, settings)
        shopify = ShopifyClient(http, tokens, settings)
        _container = Container(settings, vault, config_repo, config, http, tokens, shopify)
    return _container


def reset_container() -> None:
    global _container
    _container = None
```

`backend/app/main.py`:
```python
from fastapi import FastAPI

app = FastAPI(title="Thetavas Order Bot")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "thetavas-order-bot"}
```

`backend/api/index.py`:
```python
from app.main import app  # noqa: F401  (Vercel ASGI entrypoint)
```

`backend/vercel.json`:
```json
{
  "builds": [{ "src": "api/index.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "api/index.py" }],
  "regions": ["bom1"]
}
```

- [ ] **Step 4: Run to verify PASS** — full suite green, ruff + mypy clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: composition root, FastAPI app with /health, Vercel entrypoint (bom1)"`

---

### Task 11: Live smoke script + final sweep

**Files:**
- Create: `backend/scripts/smoke_shopify.py`, `backend/README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `python -m scripts.smoke_shopify` (run from `backend/` with `SHOPIFY_CLIENT_ID`/`SHOPIFY_CLIENT_SECRET` + `APP_MASTER_KEY` in env/.env) — read-only live checks + bogus-gid mutation validation, printing PASS/FAIL per check, token masked.

- [ ] **Step 1: Implement the script (dev tool — no unit test; the suite stays offline)**

`backend/scripts/smoke_shopify.py`:
```python
"""Dev-only live smoke test. Requires env: APP_MASTER_KEY, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET.

Read-only against the live store + mutation schema checks on a non-existent gid.
Run: python -m scripts.smoke_shopify
"""

import asyncio
import os

import httpx

from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.config.settings import Settings
from app.shopify.client import ShopifyClient
from app.shopify.errors import ShopifyGraphQLError
from app.shopify.models import AuthorizedOrder, Order
from app.shopify.token_manager import TokenManager
from app.store.memory import InMemoryConfigRepo


def _bogus_auth() -> AuthorizedOrder:
    order = Order(
        gid="gid://shopify/Order/1", name="tavas0", email=None, phone=None,
        shipping_phone=None, billing_phone=None, financial_status=None,
        fulfillment_status=None, cancelled_at=None, tags=(), payment_gateway_names=(),
        total=None, customer_locale=None,
    )
    return AuthorizedOrder(order=order, verified_phone="+910000000000")


async def main() -> None:
    settings = Settings()
    config = ConfigService(InMemoryConfigRepo(), SecretVault(settings.app_master_key))
    await config.set_secret("shopify:client_id", os.environ["SHOPIFY_CLIENT_ID"])
    await config.set_secret("shopify:client_secret", os.environ["SHOPIFY_CLIENT_SECRET"])
    async with httpx.AsyncClient() as http:
        client = ShopifyClient(http, TokenManager(http, config, settings), settings)
        token = await client._tokens.get_token()  # noqa: SLF001
        print(f"token: {token[:10]}... OK")
        latest = await client.find_order_by_name("tavas3733")
        print(f"find_order_by_name: {'OK ' + latest.name if latest else 'NOT FOUND'}")
        if latest:
            refetched = await client.get_order(latest.gid)
            print(f"get_order: {'OK' if refetched and refetched.gid == latest.gid else 'FAIL'}")
        fallback = await client.find_customer_orders_by_phone("+910000000000")
        print(f"customer fallback (expect [] until read_customers granted): {fallback}")
        for label, call in (
            ("tagsAdd bogus-gid", client.add_tags(_bogus_auth(), ["smoke-test"])),
            ("orderCancel bogus-gid", client.cancel_order(_bogus_auth())),
        ):
            try:
                await call
                print(f"{label}: UNEXPECTED SUCCESS")
            except ShopifyGraphQLError as exc:
                print(f"{label}: OK (userError as expected: {exc.messages[0]})")


if __name__ == "__main__":
    asyncio.run(main())
```

`backend/README.md`:
```markdown
# Thetavas Order Bot — Backend

Phase 1: skeleton + Shopify layer. See `docs/architecture-plan.md` (v1.1) and
`docs/architecture-decisions.md` (ADRs 001–005).

## Dev setup (from backend/)
1. `python -m pip install -r requirements.txt`
2. Create `.env`: `APP_MASTER_KEY=<Fernet key>` (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
3. `python -m pytest` · `ruff check .` · `mypy app`

## Live smoke (optional, dev-only)
Add `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` to `.env`, then:
`python -m scripts.smoke_shopify`
```

- [ ] **Step 2: Full verification sweep**

Run from `backend/`: `python -m pytest -q` (all green) · `ruff check .` · `mypy app` · secrets grep from Global Constraints (EMPTY).

- [ ] **Step 3: Run the live smoke** (only if `.env` has real creds; otherwise mark SKIPPED in the report)

Run: `python -m scripts.smoke_shopify`
Expected: token OK · find_order_by_name OK · get_order OK · fallback `[]` · both bogus mutations report "userError as expected: Order does not exist".

- [ ] **Step 4: Commit** — `git commit -m "feat: live smoke script and backend README; phase 1 complete"`

---

## Self-Review (done at plan time)

- **Coverage:** ADR-003 → Task 6; ADR-004 (type gate) → Tasks 5+9; verified conventions (phone chain, COD, tavas prefix, 2026-07, partial-data ACCESS_DENIED) → Tasks 5/7/8; cafe lessons (no secrets in errors, region, offline tests) → Tasks 6/10; ADR-001/002/005 are Phase 2+ scope — intentionally absent.
- **Placeholders:** none — every step has full code.
- **Type consistency:** `ConfigService.get_secret/set_secret/get_plain/set_plain` used identically in Tasks 4/6/11; `AuthorizedOrder(order=…, verified_phone=…)` consistent in Tasks 5/9/11; `make_client`/`seed`/`grant_or` helpers defined in Task 7's test module and imported by Tasks 8–9 tests; `make_order` defined in Task 5's tests and imported by Task 9's.
```
