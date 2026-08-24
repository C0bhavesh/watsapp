# Inbound Image Product Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A customer sending a photo on WhatsApp (e.g. "what's the price of this?") gets a real, catalog-grounded answer from the AI, and the admin operator can see that photo in the chat page — today the bot silently drops every inbound image.

**Architecture:** All new logic lives at the channel boundary. `channels/whatsapp.py`'s webhook loop gets a new branch for `InboundImage` events that downloads the image from Meta, stores it, asks Gemini (vision) for a short product description, and synthesizes a plain `InboundText` from the caption + description — then calls the **existing, unchanged** `run_turn()` exactly as for a real typed message. `core/` and `agents/` never learn images exist; `product_search`'s grounded Shopify lookup (real price/size/availability/link, no hallucination) just runs on the synthesized text like any other turn. A new Postgres table (`inbound_images`, keyed by phone + WhatsApp message id — no FK into `conversations`/`messages`, so there is no ordering dependency on when the turn's own message row gets created) holds the bytes for the one-time vision call and for later admin display via a new authenticated endpoint.

**Tech Stack:** Python 3.12+, FastAPI, httpx (new Meta Graph API calls), LiteLLM/Gemini (vision-capable, already the active model), asyncpg/Supabase Postgres (new `bytea` table), pytest + pytest-asyncio, vanilla JS (`chats.js`, no framework).

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-08-24-inbound-image-product-lookup-design.md`. Read it for the full rationale; this plan is its exact implementation.
- **Out of scope, do not touch:** exchange/damage-claim photo evidence (stays routed to human handoff, unaffected); the bot/AI sending images to the customer; video/audio/document/sticker/location inbound types (still silently dropped); widening `Message.content` (the shared text-only contract every agent uses) to be multimodal.
- **Never raise on attacker/network input.** Every new parsing/fetch/provider step in the image pipeline must degrade gracefully (return `None`, skip the image, fall back to caption-only or a fixed placeholder text) exactly like `whatsapp_inbound.py`'s existing posture for text/button/interactive parsing — never let a malformed webhook, a Meta API failure, or a vision-call failure raise out of the webhook handler.
- **Secrets:** the WhatsApp access token used for the two new Meta Graph API calls must never be logged; redact it from any error string the same way `whatsapp_sender.py::_safe_error` already does for send failures.
- **Size cap:** reject (skip storage/vision, degrade to caption-only) any downloaded image over 5 MiB (`_MAX_IMAGE_BYTES = 5 * 1024 * 1024`).
- **Mime allowlist:** only `image/jpeg`, `image/png`, `image/webp` are accepted; anything else is rejected the same way as oversized.
- **SSRF posture:** validate the Meta-provided download URL's host against an allowlist of Meta-owned domain suffixes before fetching it (mirrors `error_learnings.md` [2026-08-15]'s header-image-URL lesson and `shopify/client.py::_is_shopify_image_url`'s dot-anchored-suffix pattern) — even though the URL comes from Meta's own API response, not directly from attacker-controlled webhook input, this is defense in depth.
- **Ownership check:** the new admin image-serving endpoint must verify the requested image actually belongs to the requested thread's phone before returning it — never serve an image by id alone.
- **No schema migration runs automatically.** Add the new table to `backend/app/store/schema.sql` (this project's source-of-truth DDL) using the same idempotent `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` style already used there. The owner runs it manually against Postgres — do not attempt to run it yourself, and flag in your final report that it is pending.
- Full type hints, `ruff`/mypy-strict clean, no bare `except`, `async def` for I/O, Pydantic v2 for any new request/response models — per this project's standing Python style rules.
- `python -m pytest tests/admin/test_static_mount.py` style substring-presence tests are this repo's accepted, established pattern for testing `chats.js` (no browser/JS test runner exists) — follow it, don't introduce a new one.

---

### Task 1: `inbound_images` store layer (schema, CRUD, erasure/retention integration)

**Files:**
- Modify: `backend/app/store/schema.sql` — add the new table DDL at the end of the file, following the existing style (see `CREATE TABLE IF NOT EXISTS messages` around line 122 for the idiom).
- Modify: `backend/app/store/base.py` — add 3 new `IngestStore` Protocol methods, 2 new dataclasses, extend `DeletionResult`.
- Modify: `backend/app/store/memory.py` — implement the 3 methods on `InMemoryIngestStore` (starts at line 148); extend its `delete_by_phone` (line 535) and `purge_older_than` (line 589).
- Modify: `backend/app/store/postgres.py` — implement the 3 methods on the Postgres `IngestStore` impl (mirror `find_outbound_by_phone`, line 709); extend `delete_by_phone` (line 964) and `purge_older_than` (line 1043).
- Test: `backend/tests/store/test_chat_reads.py` (CRUD, in-memory), `backend/tests/store/test_chat_reads_pg.py` (CRUD, Postgres — gated/skips without `TEST_DATABASE_URL`, matching that file's existing pattern), `backend/tests/store/test_deletion.py` (erasure/retention extension, both stores).

**Interfaces:**
- Consumes: nothing new — this is the foundation task.
- Produces: `IngestStore.save_inbound_image(phone_e164: str, wamid: str, mime_type: str, image_bytes: bytes) -> int` (returns new row id); `IngestStore.find_inbound_images_by_phone(phone_e164: str, limit: int = 100) -> list[InboundImageEntry]`; `IngestStore.get_inbound_image(image_id: int) -> StoredInboundImage | None`; dataclasses `InboundImageEntry(id: int, mime_type: str, created_at: str | None)` and `StoredInboundImage(phone_e164: str, mime_type: str, bytes: bytes)`. `DeletionResult` gains `inbound_images: int = 0`. Tasks 5 and 6 call these.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/store/test_chat_reads.py` (mirror the file's existing `find_outbound_by_phone` test section — check its imports/fixture setup at the top, likely a bare `InMemoryIngestStore()` or similar per-test instantiation):

```python
async def test_save_and_find_inbound_images_by_phone() -> None:
    store = InMemoryIngestStore()
    image_id = await store.save_inbound_image(
        "+919664290413", "wamid.ABC123", "image/jpeg", b"\xff\xd8\xff\xe0fakejpeg"
    )
    entries = await store.find_inbound_images_by_phone("+919664290413")
    assert len(entries) == 1
    assert entries[0].id == image_id
    assert entries[0].mime_type == "image/jpeg"
    assert entries[0].created_at is not None


async def test_find_inbound_images_by_phone_no_match_returns_empty() -> None:
    store = InMemoryIngestStore()
    await store.save_inbound_image(
        "+919664290413", "wamid.ABC123", "image/jpeg", b"data"
    )
    entries = await store.find_inbound_images_by_phone("+910000000000")
    assert entries == []


async def test_get_inbound_image_returns_bytes() -> None:
    store = InMemoryIngestStore()
    image_id = await store.save_inbound_image(
        "+919664290413", "wamid.ABC123", "image/png", b"\x89PNGfakepng"
    )
    stored = await store.get_inbound_image(image_id)
    assert stored is not None
    assert stored.phone_e164 == "+919664290413"
    assert stored.mime_type == "image/png"
    assert stored.bytes == b"\x89PNGfakepng"


async def test_get_inbound_image_missing_id_returns_none() -> None:
    store = InMemoryIngestStore()
    assert await store.get_inbound_image(999) is None
```

Add the same coverage, Postgres-backed, to `backend/tests/store/test_chat_reads_pg.py` (already gated on `TEST_DATABASE_URL` via its module-level `pytestmark = pytest.mark.skipif(not DSN, ...)` and `pool` fixture — both already defined at the top of that file, reuse them as-is):

```python
async def test_save_and_find_inbound_images_by_phone_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    image_id = await store.save_inbound_image(phone, f"wamid.{uuid.uuid4()}", "image/jpeg", b"data")

    entries = await store.find_inbound_images_by_phone(phone)

    assert len(entries) == 1
    assert entries[0].id == image_id
    assert entries[0].mime_type == "image/jpeg"


async def test_get_inbound_image_returns_bytes_pg(pool: LazyPool) -> None:
    store = PostgresIngestStore(pool)
    phone = f"+91{uuid.uuid4().int % 10**10:010d}"
    image_id = await store.save_inbound_image(
        phone, f"wamid.{uuid.uuid4()}", "image/png", b"\x89PNGfakepng"
    )

    stored = await store.get_inbound_image(image_id)

    assert stored is not None
    assert stored.phone_e164 == phone
    assert stored.mime_type == "image/png"
    assert stored.bytes == b"\x89PNGfakepng"
```

This file tests `InMemoryIngestStore.delete_by_phone`/`purge_older_than` directly against real
seeded state, and separately tests the Postgres implementation's SQL surface via a fake
connection (`_FakeConn`/`_FakePool`, already defined near the bottom of the file) that records
executed statements — no live database needed. Add to the in-memory section, near the existing
`test_delete_by_phone_removes_only_that_number`:

```python
async def test_delete_by_phone_removes_inbound_images() -> None:
    store = InMemoryIngestStore()
    await store.save_inbound_image("+919111111111", "wamid.A", "image/jpeg", b"x")
    await store.save_inbound_image("+919111111111", "wamid.B", "image/jpeg", b"y")
    await store.save_inbound_image("+919222222222", "wamid.C", "image/jpeg", b"z")

    result = await store.delete_by_phone("+919111111111")

    assert result.inbound_images == 2
    assert await store.find_inbound_images_by_phone("+919111111111") == []
    assert await store.find_inbound_images_by_phone("+919222222222") != []
```

Add to the fake-connection Postgres section, near the existing `test_pg_delete_by_phone_targets_every_phone_bearing_table` and `test_pg_purge_older_than_ages_actions_and_dedupe_tables`:

```python
async def test_pg_delete_by_phone_targets_inbound_images() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.delete_by_phone("+919111111111")

    tables = _targets(conn)
    assert "inbound_images" in tables
    bound = {sql: args for sql, args in conn.calls}
    assert bound["DELETE FROM inbound_images WHERE phone_e164 = $1"] == ("+919111111111",)


async def test_pg_purge_older_than_ages_inbound_images() -> None:
    conn = _FakeConn()
    store = PostgresIngestStore(_FakePool(conn))  # type: ignore[arg-type]

    await store.purge_older_than(datetime.now(UTC))

    joined = " ".join(conn.executed)
    assert "DELETE FROM inbound_images WHERE created_at < $1" in joined
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py tests/store/test_deletion.py -v -k inbound_image`
Expected: FAIL — `save_inbound_image`/`find_inbound_images_by_phone`/`get_inbound_image` don't exist yet, and `DeletionResult` has no `inbound_images` field.

- [ ] **Step 3: Add the schema DDL**

Append to `backend/app/store/schema.sql`:

```sql
-- Inbound customer photos (product-lookup feature, 2026-08-24). Keyed by phone + WhatsApp
-- message id (wamid) -- deliberately NOT a foreign key into messages/conversations, so storing
-- the image never depends on when (or whether) the synthesized turn's own message row is
-- created; the admin thread view joins by phone at read time instead (see
-- IngestStore.find_inbound_images_by_phone). NOTE: an OWNER-RUN manual migration -- nothing in
-- the app executes schema.sql automatically; documented here as the source-of-truth DDL.
CREATE TABLE IF NOT EXISTS inbound_images (
    id          bigserial PRIMARY KEY,
    phone_e164  text NOT NULL,
    wamid       text NOT NULL,
    mime_type   text NOT NULL,
    bytes       bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inbound_images_phone ON inbound_images (phone_e164, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_inbound_images_wamid ON inbound_images (wamid);
```

- [ ] **Step 4: Add the Protocol methods and dataclasses to `store/base.py`**

Add near `OutboundEntry` (around line 99-105):

```python
@dataclass(frozen=True)
class InboundImageEntry:
    id: int
    mime_type: str
    created_at: str | None


@dataclass(frozen=True)
class StoredInboundImage:
    phone_e164: str
    mime_type: str
    bytes: bytes
```

Extend `DeletionResult` (find its field list, e.g. around line 155-162) by adding one field, keeping every existing field and its ordering/defaults untouched:

```python
    inbound_images: int = 0
```

Add to the `IngestStore` Protocol, near `find_outbound_by_phone` (around line 256):

```python
    async def save_inbound_image(
        self, phone_e164: str, wamid: str, mime_type: str, image_bytes: bytes
    ) -> int: ...

    async def find_inbound_images_by_phone(
        self, phone_e164: str, limit: int = 100
    ) -> list[InboundImageEntry]: ...

    async def get_inbound_image(self, image_id: int) -> StoredInboundImage | None: ...
```

- [ ] **Step 5: Implement on `InMemoryIngestStore` (`store/memory.py`)**

In `__init__` (around line 148-178), add:

```python
        # id -> (phone_e164, wamid, mime_type, bytes, created_at). A plain dict keyed by an
        # incrementing int id mirrors this class's other "_by_id" tables (e.g. _outbound_by_id).
        self._inbound_images: dict[int, tuple[str, str, str, bytes, datetime]] = {}
        self._inbound_images_next_id = 1
```

Add the 3 new methods (anywhere among the class's other read/write methods, e.g. near `count_orders_by_phone`):

```python
    async def save_inbound_image(
        self, phone_e164: str, wamid: str, mime_type: str, image_bytes: bytes
    ) -> int:
        image_id = self._inbound_images_next_id
        self._inbound_images_next_id += 1
        self._inbound_images[image_id] = (
            phone_e164, wamid, mime_type, image_bytes, datetime.now(UTC)
        )
        return image_id

    async def find_inbound_images_by_phone(
        self, phone_e164: str, limit: int = 100
    ) -> list[InboundImageEntry]:
        matches = [
            (image_id, mime_type, created_at)
            for image_id, (phone, _wamid, mime_type, _bytes, created_at)
            in self._inbound_images.items()
            if phone == phone_e164
        ]
        return [
            InboundImageEntry(id=image_id, mime_type=mime_type, created_at=created_at.isoformat())
            for image_id, mime_type, created_at in matches[-limit:]
        ]

    async def get_inbound_image(self, image_id: int) -> StoredInboundImage | None:
        row = self._inbound_images.get(image_id)
        if row is None:
            return None
        phone_e164, _wamid, mime_type, image_bytes, _created_at = row
        return StoredInboundImage(phone_e164=phone_e164, mime_type=mime_type, bytes=image_bytes)
```

Extend `delete_by_phone` (around line 535-587): add before its `return DeletionResult(...)`:

```python
        removed_images = [
            image_id
            for image_id, (phone, _w, _m, _b, _c) in self._inbound_images.items()
            if phone == phone_e164
        ]
        for image_id in removed_images:
            del self._inbound_images[image_id]
```

and add `inbound_images=len(removed_images),` to that `DeletionResult(...)` call.

Extend `purge_older_than` (around line 589-603): add before its `return DeletionResult(...)`:

```python
        removed_images = [
            image_id
            for image_id, (_p, _w, _m, _b, created_at) in self._inbound_images.items()
            if created_at < cutoff
        ]
        for image_id in removed_images:
            del self._inbound_images[image_id]
```

and add `inbound_images=len(removed_images),` to that `DeletionResult(...)` call.

- [ ] **Step 6: Implement on the Postgres store (`store/postgres.py`)**

Add the 3 new methods near `find_outbound_by_phone` (line 709-731), same class, matching its exact style:

```python
    async def save_inbound_image(
        self, phone_e164: str, wamid: str, mime_type: str, image_bytes: bytes
    ) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO inbound_images (phone_e164, wamid, mime_type, bytes)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                phone_e164, wamid, mime_type, image_bytes,
            )
        assert row is not None
        return int(row["id"])

    async def find_inbound_images_by_phone(
        self, phone_e164: str, limit: int = 100
    ) -> list[InboundImageEntry]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, mime_type, created_at FROM inbound_images WHERE phone_e164 = $1"
                " ORDER BY created_at DESC LIMIT $2",
                phone_e164, limit,
            )
        return [
            InboundImageEntry(
                id=int(r["id"]),
                mime_type=str(r["mime_type"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]

    async def get_inbound_image(self, image_id: int) -> StoredInboundImage | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT phone_e164, mime_type, bytes FROM inbound_images WHERE id = $1",
                image_id,
            )
        if row is None:
            return None
        return StoredInboundImage(
            phone_e164=str(row["phone_e164"]),
            mime_type=str(row["mime_type"]),
            bytes=bytes(row["bytes"]),
        )
```

Extend `delete_by_phone` (line 964-1041): add one more `conn.execute(...)` inside the existing `async with conn.transaction():` block, alongside the other DELETEs (e.g. right after the `outbound` delete):

```python
                images = await conn.execute(
                    "DELETE FROM inbound_images WHERE phone_e164 = $1", phone_e164
                )
```

and add `inbound_images=_rows_affected(images),` to the `DeletionResult(...)` return.

Extend `purge_older_than` (line 1043+): add one more `conn.execute(...)` inside its transaction block:

```python
                images = await conn.execute(
                    "DELETE FROM inbound_images WHERE created_at < $1", cutoff
                )
```

and add `inbound_images=_rows_affected(images),` to its `DeletionResult(...)` return.

- [ ] **Step 7: Run the tests**

Run: `cd backend && python -m pytest tests/store/test_chat_reads.py tests/store/test_deletion.py -v -k inbound_image`
Expected: PASS (in-memory tests). Postgres-gated tests in `test_chat_reads_pg.py`/`test_deletion.py`'s Postgres section skip cleanly without `TEST_DATABASE_URL` — if it's set in this environment, run those too and confirm they pass.

- [ ] **Step 8: Run the full backend suite and static checks**

Run: `cd backend && python -m pytest -q && ruff check . && mypy app --strict`
Expected: all green, no new failures.

- [ ] **Step 9: Commit**

```bash
git add backend/app/store/schema.sql backend/app/store/base.py backend/app/store/memory.py backend/app/store/postgres.py backend/tests/store/test_chat_reads.py backend/tests/store/test_chat_reads_pg.py backend/tests/store/test_deletion.py
git commit -m "feat(store): add inbound_images CRUD + erasure/retention integration"
```

---

### Task 2: Meta media download (`channels/whatsapp_media.py`)

**Files:**
- Create: `backend/app/channels/whatsapp_media.py`
- Test: `backend/tests/test_whatsapp_media.py` (new file)

**Interfaces:**
- Consumes: `httpx.AsyncClient` (the container's `c.http`), `WhatsAppConfig` (from `channels/whatsapp_config.py` — `access_token`, `api_version`).
- Produces: `FetchedMedia(bytes: bytes, mime_type: str)` (frozen dataclass); `async def fetch_media(http: httpx.AsyncClient, cfg: WhatsAppConfig, media_id: str, timeout: float = 20.0) -> FetchedMedia | None`. Task 5 calls this.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_whatsapp_media.py`. This mirrors `backend/tests/test_whatsapp_sender.py`'s exact style: `httpx.AsyncClient(transport=httpx.MockTransport(handler))` via a `client_with` helper, plain `async def test_...` functions with NO `@pytest.mark.asyncio` decorator (this project's pytest config runs in `asyncio_mode = "auto"`, confirmed by that file having no decorators either):

```python
import httpx

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_media import fetch_media

CFG = WhatsAppConfig(
    access_token="tok", app_secret="sec", verify_token="vtok",
    phone_number_id="123", waba_id="456", api_version="v23.0",
)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _transport(media_response: dict, download_body: bytes, download_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://graph.facebook.com/v23.0/MEDIA123":
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=media_response)
        if str(request.url) == media_response.get("url"):
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(download_status, content=download_body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_fetch_media_returns_bytes_and_mime_type() -> None:
    transport = _transport(
        {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/img1",
            "mime_type": "image/jpeg",
        },
        b"\xff\xd8\xff\xe0fakejpeg",
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is not None
    assert result.mime_type == "image/jpeg"
    assert result.bytes == b"\xff\xd8\xff\xe0fakejpeg"


async def test_fetch_media_rejects_disallowed_mime_type() -> None:
    transport = _transport(
        {"url": "https://lookaside.fbsbx.com/x", "mime_type": "application/pdf"}, b"data"
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_rejects_untrusted_host() -> None:
    transport = _transport(
        {"url": "https://evil.example.com/x", "mime_type": "image/jpeg"}, b"data"
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_rejects_oversized_download() -> None:
    transport = _transport(
        {"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg"},
        b"x" * (5 * 1024 * 1024 + 1),
    )
    async with httpx.AsyncClient(transport=transport) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None


async def test_fetch_media_returns_none_on_media_lookup_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await fetch_media(http, CFG, "MEDIA123")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_media.py -v`
Expected: FAIL — `app.channels.whatsapp_media` doesn't exist yet.

- [ ] **Step 3: Implement `whatsapp_media.py`**

```python
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.channels.whatsapp_config import WhatsAppConfig

# WhatsApp's own inbound-image size ceiling; reject anything larger rather than pay to
# store/vision-analyze an oversized payload.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
# Meta's Graph API media-download URLs resolve to one of these domain families. Dot-anchored
# suffix match (mirrors shopify/client.py::_is_shopify_image_url) so a lookalike host
# ("fbsbx.com.evil.com") is rejected, not just a substring match.
_META_MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".fbcdn.net", ".facebook.com")


@dataclass(frozen=True)
class FetchedMedia:
    bytes: bytes
    mime_type: str


def _is_trusted_meta_media_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = parts.hostname
    return host is not None and any(host.endswith(suffix) for suffix in _META_MEDIA_HOST_SUFFIXES)


async def fetch_media(
    http: httpx.AsyncClient, cfg: WhatsAppConfig, media_id: str, timeout: float = 20.0
) -> FetchedMedia | None:
    """Resolve a Meta media id to its bytes, or None on any failure/rejection.

    Two Bearer-authenticated Graph API calls: first resolves media_id -> a short-lived download
    URL + Meta's own reported mime_type, second fetches the bytes from that URL. Never raises --
    every failure mode (network error, non-200, malformed JSON, disallowed mime type, untrusted
    host, oversized body) degrades to None, mirroring whatsapp_inbound.py's "attacker/network
    input, never raise" posture.
    """
    headers = {"Authorization": f"Bearer {cfg.access_token}"}
    try:
        meta_resp = await http.get(
            f"https://graph.facebook.com/{cfg.api_version}/{media_id}",
            headers=headers, timeout=timeout,
        )
    except httpx.HTTPError:
        return None
    if meta_resp.status_code != 200:
        return None
    try:
        meta = meta_resp.json()
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    url = meta.get("url")
    mime_type = meta.get("mime_type")
    if not isinstance(url, str) or not isinstance(mime_type, str):
        return None
    if mime_type not in _ALLOWED_IMAGE_MIME_TYPES:
        return None
    if not _is_trusted_meta_media_url(url):
        return None

    try:
        data_resp = await http.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return None
    if data_resp.status_code != 200:
        return None
    content = data_resp.content
    if len(content) > _MAX_IMAGE_BYTES:
        return None
    return FetchedMedia(bytes=content, mime_type=mime_type)
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && python -m pytest tests/test_whatsapp_media.py -v`
Expected: PASS, all 5 tests.

- [ ] **Step 5: Static checks**

Run: `cd backend && ruff check app/channels/whatsapp_media.py tests/test_whatsapp_media.py && mypy app/channels/whatsapp_media.py --strict`
Expected: clean.

- [ ] **Step 6: Compliance grep**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/channels/whatsapp_media.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add backend/app/channels/whatsapp_media.py backend/tests/test_whatsapp_media.py
git commit -m "feat(channels): fetch inbound WhatsApp media with SSRF/size/mime guards"
```

---

### Task 3: `InboundImage` parsing (`channels/whatsapp_inbound.py`)

**Files:**
- Modify: `backend/app/channels/whatsapp_inbound.py`
- Test: `backend/tests/test_whatsapp_inbound.py` (existing file — add new test functions near the existing `InboundButton`/`InboundInteractive` parsing tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `InboundImage(message_id: str, wa_id: str, media_id: str, mime_type: str, caption: str | None, timestamp: str)` (frozen dataclass); `InboundEvent` union gains `InboundImage`. Tasks 5 and the `channels/whatsapp.py` webhook loop (Task 5) consume this.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_whatsapp_inbound.py` (match its existing test style/fixtures for building a minimal webhook `payload` dict around a single message — check an existing `InboundButton` parsing test for the exact envelope shape to copy):

```python
def test_extract_events_parses_image_message_with_caption() -> None:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IMG1",
                        "from": "919664290413",
                        "timestamp": "1700000000",
                        "type": "image",
                        "image": {
                            "id": "MEDIA123",
                            "mime_type": "image/jpeg",
                            "caption": "do you have this in size M?",
                        },
                    }]
                }
            }]
        }]
    }
    events = extract_events(payload)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InboundImage)
    assert event.message_id == "wamid.IMG1"
    assert event.wa_id == "919664290413"
    assert event.media_id == "MEDIA123"
    assert event.mime_type == "image/jpeg"
    assert event.caption == "do you have this in size M?"


def test_extract_events_parses_image_message_without_caption() -> None:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IMG2",
                        "from": "919664290413",
                        "timestamp": "1700000000",
                        "type": "image",
                        "image": {"id": "MEDIA456", "mime_type": "image/png"},
                    }]
                }
            }]
        }]
    }
    events = extract_events(payload)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InboundImage)
    assert event.caption is None


def test_extract_events_drops_image_message_missing_media_id() -> None:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.IMG3",
                        "from": "919664290413",
                        "timestamp": "1700000000",
                        "type": "image",
                        "image": {"mime_type": "image/jpeg"},
                    }]
                }
            }]
        }]
    }
    assert extract_events(payload) == []
```

(Add `InboundImage` to that file's existing import line from `app.channels.whatsapp_inbound`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_inbound.py -v -k image`
Expected: FAIL — `InboundImage` doesn't exist, `msg_type == "image"` isn't handled.

- [ ] **Step 3: Add the dataclass and parsing branch**

In `backend/app/channels/whatsapp_inbound.py`, add after `InboundButton` (line 22-29):

```python
@dataclass(frozen=True)
class InboundImage:
    message_id: str
    wa_id: str
    media_id: str
    mime_type: str
    caption: str | None
    timestamp: str
```

Change line 44:

```python
InboundEvent = InboundText | InboundInteractive | InboundButton | InboundImage
```

In `_parse_message` (after the `msg_type == "interactive"` block, before the final `return None` at line 178), add:

```python
    if msg_type == "image":
        image_obj = msg.get("image")
        if not isinstance(image_obj, dict):
            return None
        media_id = image_obj.get("id")
        mime_type = image_obj.get("mime_type")
        if not isinstance(media_id, str) or not isinstance(mime_type, str):
            return None
        caption = image_obj.get("caption")
        return InboundImage(
            message_id=message_id,
            wa_id=wa_id,
            media_id=media_id,
            mime_type=mime_type,
            caption=caption if isinstance(caption, str) else None,
            timestamp=timestamp_str,
        )
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && python -m pytest tests/test_whatsapp_inbound.py -v`
Expected: PASS, including every pre-existing test in this file (no regression on text/button/interactive parsing).

- [ ] **Step 5: Static checks**

Run: `cd backend && ruff check app/channels/whatsapp_inbound.py tests/test_whatsapp_inbound.py && mypy app/channels/whatsapp_inbound.py --strict`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/channels/whatsapp_inbound.py backend/tests/test_whatsapp_inbound.py
git commit -m "feat(channels): parse inbound WhatsApp image messages"
```

---

### Task 4: Vision description (`providers/litellm_provider.py` + `providers/base.py`)

**Files:**
- Modify: `backend/app/providers/base.py` — add `describe_image` to the `LLMProvider` Protocol.
- Modify: `backend/app/providers/litellm_provider.py` — implement `describe_image` on `LiteLLMProvider`; extract shared auth/error-handling out of `complete()` into two small helpers so `describe_image` reuses them instead of duplicating ~15 lines (DRY — both methods hit the exact same Gemini/Vertex auth branching and error classification).
- Test: `backend/tests/providers/test_litellm_provider.py` (existing file — add near its existing `complete()` tests).

**Interfaces:**
- Consumes: nothing new (uses this module's existing `_classify`, `_redact`, `VertexConfig`, `ProviderError`, `ProviderErrorKind`).
- Produces: `LLMProvider.describe_image(image_bytes: bytes, mime_type: str, api_key: str, model: str, timeout: float, *, extra_params: dict[str, object] | None = None) -> str`. Task 5 calls this via `app.deps.active_llm(...)`'s returned provider.

- [ ] **Step 1: Write the failing tests**

This file's established pattern (see its existing `test_vertex_model_injects_vertex_creds_not_api_key`) injects a fake module into `sys.modules["litellm"]` via `monkeypatch.setitem` — NOT `monkeypatch.setattr("litellm.acompletion", ...)`, since `litellm` is imported lazily *inside* `complete()`/`describe_image()` and a string-path `setattr` would trigger a real import of the (heavy) actual package. Reuse the file's existing `_FakeLiteLLM`/`_FakeResp`/`_FakeChoice`/`_FakeMessage` classes (already defined at the top of the file) — add these two tests using that exact fixture:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/providers/test_litellm_provider.py -v -k describe_image`
Expected: FAIL — `describe_image` doesn't exist on `LiteLLMProvider`.

- [ ] **Step 3: Add `describe_image` to the `LLMProvider` Protocol**

In `backend/app/providers/base.py`, add to the `LLMProvider` Protocol (after `complete`):

```python
    async def describe_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        api_key: str,
        model: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> str: ...
```

- [ ] **Step 4: Extract shared auth/error helpers and implement `describe_image` in `litellm_provider.py`**

Replace the body of `complete` (lines 60-79, the auth-branching block only — from `call_kwargs: dict[str, object] = dict(extra_params or {})` through the `else: call_kwargs["api_key"] = api_key` line) and its error handling (lines 84-89) by extracting two methods, then add `describe_image`. The full resulting file section (replacing from `class LiteLLMProvider:` at line 48 to the end of `complete` at line 94):

```python
class LiteLLMProvider:
    def __init__(self, vertex: VertexConfig | None = None) -> None:
        self._vertex = vertex

    def _auth_kwargs(self, model: str, api_key: str) -> dict[str, object]:
        if model.startswith("vertex_ai/"):
            v = self._vertex
            if v is None or not v.credentials_json or not v.project:
                raise ProviderError(
                    "Vertex AI credentials are not configured", ProviderErrorKind.AUTH
                )
            # Vertex authenticates with the service-account JSON + project + location,
            # NOT an api_key — omit api_key entirely for vertex_ai/* models.
            return {
                "vertex_credentials": v.credentials_json,
                "vertex_project": v.project,
                "vertex_location": v.location,
            }
        return {"api_key": api_key}

    def _wrap_error(self, exc: Exception, model: str, api_key: str) -> ProviderError:
        if model.startswith("vertex_ai/"):
            # A vertex error may embed the service-account JSON (exact, reformatted, or a
            # lone field) — discard the raw text entirely and surface a fixed safe message.
            return ProviderError("Vertex AI request failed", _classify(exc))
        return ProviderError(_redact(str(exc), api_key), _classify(exc))

    async def complete(
        self,
        model: str,
        messages: list[Message],
        api_key: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> CompletionResult:
        import litellm  # lazy: never on the webhook cold path

        # httpx transport avoids stale-keepalive spurious timeouts on Vercel (cafe fix)
        litellm.disable_aiohttp_transport = True
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        call_kwargs: dict[str, object] = dict(extra_params or {})
        call_kwargs.update(self._auth_kwargs(model, api_key))
        try:
            resp = await litellm.acompletion(
                model=model, messages=msg_dicts, timeout=timeout, **call_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — every upstream error becomes ProviderError
            raise self._wrap_error(exc, model, api_key) from exc
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        return CompletionResult(text=text, model=model)

    async def describe_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        api_key: str,
        model: str,
        timeout: float,
        *,
        extra_params: dict[str, object] | None = None,
    ) -> str:
        import base64

        import litellm  # lazy: mirrors complete()'s posture

        litellm.disable_aiohttp_transport = True
        b64 = base64.b64encode(image_bytes).decode("ascii")
        msg_dicts = [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ],
        }]
        call_kwargs: dict[str, object] = dict(extra_params or {})
        call_kwargs.update(self._auth_kwargs(model, api_key))
        try:
            resp = await litellm.acompletion(
                model=model, messages=msg_dicts, timeout=timeout, **call_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(exc, model, api_key) from exc
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        return str(text)
```

Add the prompt constant near the top of the file (after the `_STATUS_TO_KIND` dict, around line 17):

```python
_VISION_PROMPT = (
    "Describe this product photo concisely for a product search: item type, color, pattern, "
    "material, and any notable features. If there is visible text, a price, or a size label, "
    "transcribe it. Do not guess a brand unless it is clearly visible. One or two sentences."
)
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && python -m pytest tests/providers/test_litellm_provider.py -v`
Expected: PASS, including every pre-existing `complete()` test in this file (the extraction must not change `complete`'s behavior — same call_kwargs shape, same error wrapping).

- [ ] **Step 6: Static checks**

Run: `cd backend && ruff check app/providers/base.py app/providers/litellm_provider.py tests/providers/test_litellm_provider.py && mypy app/providers/base.py app/providers/litellm_provider.py --strict`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/providers/base.py backend/app/providers/litellm_provider.py backend/tests/providers/test_litellm_provider.py
git commit -m "feat(providers): add vision-based describe_image, extract shared auth/error helpers"
```

---

### Task 5: Image intake orchestration + webhook wiring

**Files:**
- Create: `backend/app/channels/whatsapp_image_intake.py`
- Modify: `backend/app/channels/whatsapp.py` — new `InboundImage` branch in `receive_webhook`'s event loop.
- Test: `backend/tests/test_whatsapp_image_intake.py` (new), `backend/tests/test_whatsapp_webhook.py` (existing — add an integration-level test for the new branch).

**Interfaces:**
- Consumes: `fetch_media` (Task 2), `InboundImage` (Task 3), `IngestStore.save_inbound_image` (Task 1), `LLMProvider.describe_image` (Task 4), `app.deps.active_llm`, `app.core.phone.normalize_phone`, `app.core.conversation.run_turn` (unchanged), `InboundText` (unchanged).
- Produces: `async def handle_inbound_image(c: Container, cfg: WhatsAppConfig, event: InboundImage) -> InboundText`. Consumed only by `channels/whatsapp.py`'s webhook loop, added in this same task.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_whatsapp_image_intake.py`, using the real in-memory container (`get_container()`/`reset_container()`, the same pattern `test_whatsapp_webhook.py`'s own `_fresh` fixture uses) with `fetch_media` and `active_llm` monkeypatched at their call sites inside the `whatsapp_image_intake` module:

```python
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
def _reset() -> None:
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
    result = await handle_inbound_image(c, _CFG, event)

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
    result = await handle_inbound_image(c, _CFG, event)

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
    result = await handle_inbound_image(c, _CFG, event)

    assert result.text.strip() != ""
    assert "photo" in result.text.lower()
```

Add one integration test to `backend/tests/test_whatsapp_webhook.py`, mirroring its existing `test_post_text_event_live_mode_uses_llm_pipeline_end_to_end` (reuses that file's `envelope`/`sign`/`post`/`FakeProvider`/`_fake_active_llm` helpers, already defined at its top). `handle_inbound_image` itself is unit-tested in Task 5's own `test_whatsapp_image_intake.py`, so it's stubbed here to isolate proving the webhook loop's new branch actually wires its output into `run_turn`:

```python
async def test_post_image_event_calls_handle_inbound_image_then_run_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.whatsapp_inbound import InboundText

    async def fake_handle_inbound_image(c, cfg, event):
        assert event.media_id == "MEDIA1"
        return InboundText(
            message_id=event.message_id, wa_id=event.wa_id,
            text="what's the price? [Photo — appears to show: a black hoodie]",
            timestamp=event.timestamp,
        )

    monkeypatch.setattr("app.channels.whatsapp.handle_inbound_image", fake_handle_inbound_image)

    sent: dict[str, object] = {}

    async def fake_send_text(http, cfg, to, body, timeout=20.0):
        from app.channels.whatsapp_sender import SendResult
        sent["body"] = body
        return SendResult(ok=True, status_code=200, wamid="x", error=None)

    monkeypatch.setattr("app.core.conversation.send_text", fake_send_text)

    provider = FakeProvider(
        responses=[
            json.dumps({"intent": "product_search"}),
            json.dumps({"reply": "That hoodie is Rs. 1499, in stock in M/L."}),
        ]
    )
    monkeypatch.setattr("app.core.conversation.active_llm", _fake_active_llm(provider))

    from app.admin.controls import AdminControls, save_controls
    from app.deps import get_container

    await save_controls(get_container().config, AdminControls(send_mode="live"))

    body = json.dumps(
        envelope({
            "from": "919999999999",
            "id": "wamid.img1",
            "timestamp": "1",
            "type": "image",
            "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "what's the price?"},
        })
    ).encode()
    resp = await post(body, {"X-Hub-Signature-256": sign(body)})

    assert resp.status_code == 200
    # results[] reports the ORIGINAL parsed event's type (InboundImage), not the synthesized
    # InboundText run_turn actually received -- matching receive_webhook's existing behavior of
    # reporting on `event`, never the downstream-transformed value.
    assert resp.json()["results"][0]["event_type"] == "InboundImage"
    assert len(provider.calls) == 2  # router classify + product_search's own completion
    assert sent["body"] == "That hoodie is Rs. 1499, in stock in M/L."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_image_intake.py -v`
Expected: FAIL — `app.channels.whatsapp_image_intake` doesn't exist yet.

- [ ] **Step 3: Implement `whatsapp_image_intake.py`**

```python
import logging

from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_inbound import InboundImage, InboundText
from app.channels.whatsapp_media import FetchedMedia, fetch_media
from app.core.phone import normalize_phone
from app.deps import Container, active_llm
from app.providers.base import ProviderError

logger = logging.getLogger("app.channels.whatsapp_image_intake")


async def handle_inbound_image(
    c: Container, cfg: WhatsAppConfig, event: InboundImage
) -> InboundText:
    """Turn an inbound image into a synthesized InboundText for run_turn.

    Downloads + stores the image and asks the active LLM to describe it (vision), then combines
    that description with the customer's caption (if any) into one plain-text message. Never
    raises: any failure (download, storage, or vision) degrades to using the caption alone, or a
    fixed placeholder if there is no caption either, so an inbound image always produces SOME
    turn instead of silently vanishing.
    """
    phone = normalize_phone(event.wa_id) or event.wa_id
    description: str | None = None

    media: FetchedMedia | None = await fetch_media(c.http, cfg, event.media_id)
    if media is not None:
        try:
            await c.ingest.save_inbound_image(
                phone, event.message_id, media.mime_type, media.bytes
            )
        except Exception:
            logger.exception("failed to persist inbound image; continuing without storage")

        llm = await active_llm(c.settings, c.config)
        if llm is not None:
            provider, model, api_key, _extra_params = llm
            try:
                description = await provider.describe_image(
                    media.bytes, media.mime_type, api_key, model, timeout=20.0
                )
            except ProviderError:
                logger.exception("vision description failed; continuing without it")

    parts: list[str] = []
    if event.caption:
        parts.append(event.caption)
    if description:
        parts.append(f"[Photo — appears to show: {description}]")
    if not parts:
        parts.append("[Customer sent a photo, but it could not be processed]")

    return InboundText(
        message_id=event.message_id,
        wa_id=event.wa_id,
        text="\n\n".join(parts),
        timestamp=event.timestamp,
    )
```

- [ ] **Step 4: Wire the new branch into `channels/whatsapp.py`**

Change the import block (lines 11-17) to add `InboundImage`:

```python
from app.channels.whatsapp_inbound import (
    InboundButton,
    InboundImage,
    InboundInteractive,
    InboundText,
    extract_events,
    extract_statuses,
)
from app.channels.whatsapp_image_intake import handle_inbound_image
```

In the event loop (around line 143-183), add a new branch after the `InboundText` branch (line 147-156) and before the `InboundButton`/`InboundInteractive` branch:

```python
            elif isinstance(event, InboundImage):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "request budget spent; skipping image turn for %s remaining "
                        "message(s) in this delivery",
                        len(events) - processed + 1,
                    )
                else:
                    synthesized = await handle_inbound_image(c, cfg, event)
                    await run_turn(c, synthesized, budget_seconds=remaining)
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && python -m pytest tests/test_whatsapp_image_intake.py tests/test_whatsapp_webhook.py -v`
Expected: PASS, including every pre-existing test in `test_whatsapp_webhook.py` (no regression on text/button/interactive webhook handling).

- [ ] **Step 6: Run the full backend suite and static checks**

Run: `cd backend && python -m pytest -q && ruff check . && mypy app --strict`
Expected: all green.

- [ ] **Step 7: Compliance grep**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/channels/whatsapp_image_intake.py backend/app/channels/whatsapp.py`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/channels/whatsapp_image_intake.py backend/app/channels/whatsapp.py backend/tests/test_whatsapp_image_intake.py backend/tests/test_whatsapp_webhook.py
git commit -m "feat(channels): wire inbound images into the existing text-turn pipeline"
```

---

### Task 6: Admin image endpoint + thread-view merge

**Files:**
- Modify: `backend/app/admin/router.py` — new `GET /admin/conversations/{thread_id}/images/{image_id}` endpoint; merge `customer_image` entries into `get_conversation_thread` (around line 932-980).
- Test: `backend/tests/admin/test_views.py` (existing file — add near its other `get_conversation_thread`/thread-merge tests, e.g. near line 490-509).

**Interfaces:**
- Consumes: `IngestStore.find_inbound_images_by_phone`, `IngestStore.get_inbound_image` (Task 1); existing `c.conversations.get_user_id`.
- Produces: the new endpoint and the `customer_image` entry shape (`{"type": "customer_image", "timestamp": ..., "image_id": int, "mime_type": str}`) that Task 7's `chats.js` renders.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/admin/test_views.py` (match the file's existing `login`/`_send_ai_message`/`_thread_id_for` helpers and `asyncio.run(...)` idiom, visible around its existing thread-merge test near line 490-509):

```python
def test_conversation_thread_includes_customer_image_entry(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919664290413", "wamid.IMG1", "image/jpeg", b"\xff\xd8\xff\xe0fakejpeg"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_id}")
    entries = resp.json()["entries"]
    image_entry = next(e for e in entries if e["type"] == "customer_image")
    assert image_entry["image_id"] == image_id
    assert image_entry["mime_type"] == "image/jpeg"


def test_get_conversation_image_returns_bytes(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919664290413", "wamid.IMG1", "image/png", b"\x89PNGfakepng"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_id}/images/{image_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNGfakepng"


def test_get_conversation_image_404s_for_wrong_thread(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    _send_ai_message("+919876500000", "hi", "hello there")
    thread_a = _thread_id_for(client, "+919664290413")

    async def _save_image() -> int:
        return await get_container().ingest.save_inbound_image(
            "+919876500000", "wamid.IMG2", "image/jpeg", b"other-customer-photo"
        )

    image_id = asyncio.run(_save_image())

    resp = client.get(f"/admin/conversations/{thread_a}/images/{image_id}")
    assert resp.status_code == 404


def test_get_conversation_image_404s_for_missing_id(client: TestClient) -> None:
    login(client)
    _send_ai_message("+919664290413", "hi", "hello there")
    thread_id = _thread_id_for(client, "+919664290413")

    resp = client.get(f"/admin/conversations/{thread_id}/images/999999")
    assert resp.status_code == 404


def test_get_conversation_image_404s_for_unknown_thread(client: TestClient) -> None:
    login(client)
    resp = client.get("/admin/conversations/999999/images/1")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/admin/test_views.py -v -k "customer_image or conversation_image"`
Expected: FAIL — the endpoint doesn't exist (404 on a route that isn't registered is a different failure than an assertion mismatch; either way these should currently fail) and `customer_image` never appears in `entries`.

- [ ] **Step 3: Merge `customer_image` entries into `get_conversation_thread`**

In `backend/app/admin/router.py`, inside `get_conversation_thread` (around line 932-980), add a new loop after the `button_tap` loop (after line 975) and before the `entries.sort(...)` call (line 979):

```python
    for img in await c.ingest.find_inbound_images_by_phone(user_id, limit=200):
        entries.append({
            "type": "customer_image",
            "timestamp": img.created_at,
            "image_id": img.id,
            "mime_type": img.mime_type,
        })
```

- [ ] **Step 4: Add the image-serving endpoint**

Add a new route near `get_conversation_thread`:

```python
@admin_router.get(
    "/conversations/{thread_id}/images/{image_id}", dependencies=[Depends(require_admin)]
)
async def get_conversation_image(thread_id: int, image_id: int) -> Response:
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    image = await c.ingest.get_inbound_image(image_id)
    # Ownership check: an image id belonging to a DIFFERENT thread's phone must never be
    # revealed via this thread's id.
    if image is None or image.phone_e164 != user_id:
        raise HTTPException(status_code=404, detail="image not found")
    return Response(content=image.bytes, media_type=image.mime_type)
```

(`Response`, `HTTPException`, `Depends`, `require_admin`, `get_container` are all already imported/defined in this file — verify and reuse, don't re-import.)

- [ ] **Step 5: Run the tests**

Run: `cd backend && python -m pytest tests/admin/test_views.py -v`
Expected: PASS, including every pre-existing test in this file.

- [ ] **Step 6: Run the full backend suite and static checks**

Run: `cd backend && python -m pytest -q && ruff check . && mypy app --strict`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/router.py backend/tests/admin/test_views.py
git commit -m "feat(admin): serve inbound customer images, merge into the chat thread view"
```

---

### Task 7: Admin chat page renders customer images (`chats.js` + `chats.html`)

**Files:**
- Modify: `backend/app/admin/static/chats.js` — `renderBubble` (line 298-330) gains an image branch.
- Modify: `backend/app/admin/static/chats.html` — one new CSS rule for `.bubble-image`.
- Test: `backend/tests/admin/test_static_mount.py` (existing file — add a substring-presence test, following that file's established pattern).

**Interfaces:**
- Consumes: the `customer_image` entry shape from Task 6 (`{type: "customer_image", timestamp, image_id, mime_type}`); the existing global `currentThreadId`.
- Produces: nothing consumed elsewhere — this is the terminal task.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/admin/test_static_mount.py`:

```python
def test_chats_js_renders_customer_image_bubbles(client: TestClient) -> None:
    # Customer-sent photos (inbound image product lookup) must render as an <img>, not as text
    # (renderBubble's default `text.textContent = entry.text` would otherwise show the literal
    # string "undefined", since customer_image entries carry no `text` field).
    js = client.get("/admin/ui/chats.js").text
    assert '"customer_image"' in js
    assert "bubble-image" in js
    assert "/images/" in js
    html = client.get("/admin/ui/chats.html").text
    assert "bubble-image" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py::test_chats_js_renders_customer_image_bubbles -v`
Expected: FAIL.

- [ ] **Step 3: Update `renderBubble` in `chats.js`**

Replace the full current `renderBubble` function (lines 298-330):

```js
function renderBubble(entry) {
  const div = document.createElement("div");
  const side =
    entry.type === "customer_message" || entry.type === "customer_image"
      ? "bubble-in"
      : "bubble-out";
  div.className = "bubble " + side;
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent =
    entry.type === "ai_reply" && entry.sender === "admin" ? "you" : entry.type.replace("_", " ");
  const ts = document.createElement("div");
  ts.className = "bubble-ts";
  ts.textContent = formatBubbleTime(entry.timestamp);
  const mark = renderDeliveryMark(entry);
  if (mark) ts.appendChild(mark);
  div.appendChild(label);
  if (entry.type === "customer_image") {
    const img = document.createElement("img");
    img.className = "bubble-image";
    img.src = "/admin/conversations/" + currentThreadId + "/images/" + entry.image_id;
    img.alt = "customer photo";
    div.appendChild(img);
  } else {
    const text = document.createElement("div");
    text.textContent = entry.text;
    div.appendChild(text);
  }
  if (entry.status && entry.status !== "sent" && entry.status !== "processing") {
    const status = document.createElement("div");
    status.className = "bubble-status";
    if (
      entry.status === "failed" ||
      entry.status === "undeliverable" ||
      entry.status === "suppressed"
    ) {
      status.classList.add("bubble-status-error");
    }
    status.textContent = STATUS_LABELS[entry.status] || entry.status;
    div.appendChild(status);
  }
  div.appendChild(ts);
  return div;
}
```

- [ ] **Step 4: Add the CSS rule to `chats.html`**

Find the existing `<style>` block in `chats.html` (it already has bubble-related rules — e.g. `.bubble`, `.bubble-in`, `.bubble-out`, `.date-divider` per prior features). Add, near the other `.bubble-*` rules:

```css
.bubble-image {
  max-width: 240px;
  border-radius: 8px;
  display: block;
  margin-top: 4px;
}
```

- [ ] **Step 5: Run the test**

Run: `cd backend && python -m pytest tests/admin/test_static_mount.py -v`
Expected: PASS, including every pre-existing test in this file (no regression on the bubble rendering for other entry types).

- [ ] **Step 6: Manual verification**

Not covered by the substring test above (this file has no browser test runner — accepted, pre-existing limitation). Before considering this task done: run the app locally, send a test image to the WhatsApp number (or seed one directly via `c.ingest.save_inbound_image` against a running local instance), open `/admin/ui/chats.html` for that thread, and confirm the photo renders inline in the message list at a reasonable size, on the correct (incoming) side of the thread.

- [ ] **Step 7: Commit**

```bash
git add backend/app/admin/static/chats.js backend/app/admin/static/chats.html backend/tests/admin/test_static_mount.py
git commit -m "feat(admin): render customer-sent images in the chat thread view"
```

---

## Manual Verification (after all tasks)

1. **Schema migration:** the owner must run the new `inbound_images` DDL from `backend/app/store/schema.sql` against the production Supabase Postgres database before this feature can work against real data — nothing in the app runs it automatically. Flag this clearly as a pending CHECKPOINT, matching how prior schema-touching features in this project's history were handled (see `docs/FR/_pipeline_status.md`'s existing CHECKPOINT entries for the pattern).
2. Send a real WhatsApp image (a product photo) with a caption like "what's the price of this?" to the bot's number and confirm: (a) it replies with a grounded answer referencing real catalog data (not a hallucinated price); (b) the image appears in the admin chat page for that thread; (c) send another image with no caption and confirm it still gets a sensible reply and still renders in the admin page.
3. Confirm the Meta media-download host allowlist (`_META_MEDIA_HOST_SUFFIXES` in `whatsapp_media.py`) actually matches the host Meta's Graph API returns in production — this plan's SSRF allowlist was written from documented Meta CDN domain families, not verified against a live Graph API response; if the real response uses a different host, `fetch_media` will silently reject every image (degrading to caption-only text, never a crash, but silently losing the vision feature) until the suffix list is corrected.
4. Confirm `POST /admin/erasure` for a phone with a saved inbound image actually removes it (Task 1's `delete_by_phone` extension) — the existing `tests/admin/test_erasure.py` suite should be re-read to see if it needs a new assertion for `inbound_images` in its response-shape checks (out of scope for this plan's tasks to modify, since erasure's own test file wasn't in the file list above; flag this as a follow-up if that file asserts against `DeletionResult`'s exact field set anywhere).
