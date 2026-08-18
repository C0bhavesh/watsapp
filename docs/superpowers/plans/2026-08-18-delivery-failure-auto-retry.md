# Delivery-Failure Auto-Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When WhatsApp reports a message failed to deliver, automatically resend it up to 3 times (each retry triggered by the next independent failure report, no artificial delay), and alert the owner once retries are exhausted.

**Architecture:** Widen the existing delivery-status-apply methods (from the delivery-receipts feature) to report whether a write was genuinely newly-applied (not a duplicate/regressive report), so retries only fire on a genuinely new failure. Add a `retry_count` column and retry-info/record store methods to both tracked tables. Add a new `app/core/delivery_retry.py` module that resends using the exact original content and the existing send-mode kill-switch gating, and alerts the owner on exhaustion. Wire it into the existing `apply_delivery_status()` orchestrator.

**Tech Stack:** Python 3.12 / FastAPI, asyncpg (Postgres) + in-memory dev store (dual-implementation), pytest.

## Global Constraints

- `backend/app/core/order_actions.py` is never touched by any task in this plan.
- Every `IngestStore`/`ConversationStore` method is implemented identically in `postgres.py` and `memory.py` (dual-store convention).
- Full type hints, `mypy`/`ruff` clean, no bare `except`, `async def` for I/O.
- Resends go through the exact same `send_decision(controls.send_mode, controls.allowlist_phones, phone)` kill-switch gating as every other outbound send — no bypass.
- Design source of truth: `docs/superpowers/specs/2026-08-18-delivery-failure-auto-retry-design.md`.
- This touches webhook-processing code — a `security-reviewer` pass is required after `code-reviewer`, same as the delivery-receipts feature.
- **Schema change required, NOT part of any task below** — the owner must run this against production Postgres manually:
  ```sql
  ALTER TABLE outbound_messages ADD COLUMN IF NOT EXISTS retry_count int NOT NULL DEFAULT 0;
  ALTER TABLE messages ADD COLUMN IF NOT EXISTS retry_count int NOT NULL DEFAULT 0;
  ```
  Tests use the in-memory store and are unaffected by whether this has been run yet — but as with the delivery-receipts feature, deploying this code before the migration runs WILL break the hot message-read path once the new columns are read by the store methods below. Flag this explicitly at handoff, exactly as was done for the delivery-receipts feature.

---

### Task 1: Widen `apply_*_delivery_status` to report whether the write was newly applied

**Files:**
- Modify: `backend/app/store/base.py` (`IngestStore.apply_outbound_delivery_status`, `ConversationStore.apply_message_delivery_status` — return type change)
- Modify: `backend/app/store/postgres.py` (both methods)
- Modify: `backend/app/store/memory.py` (both methods)
- Modify: `backend/app/core/apply_status.py` (`apply_delivery_status` orchestrator — consume the new return type)
- Test: `backend/tests/store/test_chat_reads.py`, `backend/tests/store/test_chat_reads_pg.py`, wherever `apply_outbound_delivery_status` is tested, `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: both methods now return `str`, one of `"not_found"`, `"applied"`, or `"unchanged"` (was `bool`: found/not-found). `"applied"` means this call's write genuinely changed `delivery_status` (a real forward transition or a fresh `failed`); `"unchanged"` means a row was found but the ordering guard rejected the write (a duplicate report, or an already-terminal `failed` row receiving another `failed`). Consumed by Task 5 (retry-trigger wiring), which must only trigger a retry on `"applied"` + `status == "failed"` — never on `"unchanged"`, or a redelivered duplicate webhook would fire a second unwanted retry for the same failure.

**Why this is needed:** Meta redelivers webhooks for reliability. Without this change, a duplicate `failed` status report for the same wamid would look identical to a genuinely new failure and trigger a second, unwanted retry.

- [ ] **Step 1: Write the failing tests**

Read `backend/app/store/postgres.py`'s CURRENT `apply_outbound_delivery_status`/`apply_message_delivery_status` implementations first (they were rewritten in the delivery-receipts feature's final-review fix wave to use a single atomic guarded UPDATE with `RETURNING id` — this task builds directly on that shape, not the original two-step version). Read the existing tests for these two methods (in `test_chat_reads.py`/`test_chat_reads_pg.py`/wherever `apply_outbound_delivery_status` is tested) to see their current assertions — most will need updating since the return type changes from `bool` to `str`.

Update/add tests asserting:
```python
async def test_apply_outbound_delivery_status_returns_applied_on_genuine_new_status(store) -> None:
    # ... seed a row with a known wamid via this file's existing helper ...
    result = await store.apply_outbound_delivery_status("wamid.NEW1", "delivered")
    assert result == "applied"


async def test_apply_outbound_delivery_status_returns_not_found_for_unknown_wamid(store) -> None:
    result = await store.apply_outbound_delivery_status("wamid.NEVER_SEEN", "delivered")
    assert result == "not_found"


async def test_apply_outbound_delivery_status_returns_unchanged_on_regression(store) -> None:
    # ... seed a row, apply "read", then attempt "delivered" (a regression the guard rejects) ...
    await store.apply_outbound_delivery_status("wamid.OUT1", "read")
    result = await store.apply_outbound_delivery_status("wamid.OUT1", "delivered")
    assert result == "unchanged"


async def test_apply_outbound_delivery_status_returns_unchanged_on_duplicate_failed(store) -> None:
    # ... seed a row, apply "failed", then apply "failed" AGAIN (Meta redelivery) ...
    first = await store.apply_outbound_delivery_status("wamid.OUT2", "failed")
    second = await store.apply_outbound_delivery_status("wamid.OUT2", "failed")
    assert first == "applied"
    assert second == "unchanged"
```
Mirror these exactly for `apply_message_delivery_status` against the `messages` table. Also update `backend/app/core/apply_status.py`'s own tests (in `test_whatsapp_webhook.py`) if any currently assert on the old boolean-found semantics.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/ -k "apply_outbound_delivery_status or apply_message_delivery_status" -v`
Expected: FAIL — old tests assert `is True`/`is False`, new ones expect string values.

- [ ] **Step 3: Implement — `base.py`**

Change both Protocol method signatures:
```python
    async def apply_outbound_delivery_status(self, wamid: str, status: str) -> str: ...
```
```python
    async def apply_message_delivery_status(self, wamid: str, status: str) -> str: ...
```
Add a short comment above each documenting the three return values (`"not_found"`/`"applied"`/`"unchanged"`) and why the distinction exists (duplicate-webhook-safe retry triggering).

- [ ] **Step 4: Implement — `postgres.py`**

Read the CURRENT implementation of both methods first (post-fix-wave shape: existence check via `fetchval`, then a single atomic guarded `UPDATE ... RETURNING id`). Change the return value: instead of `return True` unconditionally after the guarded UPDATE, capture the UPDATE's `RETURNING id` result and return `"applied"` if a row came back, `"unchanged"` if not (the guard rejected it), `"not_found"` if the initial existence check found nothing. Example shape for `apply_outbound_delivery_status`:
```python
    async def apply_outbound_delivery_status(self, wamid: str, status: str) -> str:
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM outbound_messages WHERE template_wamid = $1", wamid
            )
            if exists is None:
                return "not_found"
            applied_id = await conn.fetchval(
                "UPDATE outbound_messages SET delivery_status = $2, updated_at = now()"
                " WHERE template_wamid = $1"
                "   AND delivery_status IS DISTINCT FROM 'failed'"
                "   AND ("
                "     $2 = 'failed'"
                "     OR ("
                "       $2 IN ('sent', 'delivered', 'read')"
                "       AND ("
                "         delivery_status IS NULL"
                "         OR (CASE delivery_status WHEN 'sent' THEN 0 WHEN 'delivered' THEN 1 WHEN 'read' THEN 2 ELSE -1 END)"
                "            < (CASE $2 WHEN 'sent' THEN 0 WHEN 'delivered' THEN 1 WHEN 'read' THEN 2 ELSE -1 END)"
                "       )"
                "     )"
                "   )"
                " RETURNING id",
                wamid, status,
            )
        return "applied" if applied_id is not None else "unchanged"
```
Mirror the equivalent for `apply_message_delivery_status` against `messages`/`wamid`. The WHERE clause's guard logic is unchanged from the existing implementation — only the return value handling changes.

- [ ] **Step 5: Implement — `memory.py`**

Read the CURRENT implementations first. Change from `return True`/`return False` to returning the three string values: `"not_found"` when no row matches the wamid; `"applied"` when found and `should_apply_delivery_status` says to write (and the write happens); `"unchanged"` when found but the guard says not to write.

- [ ] **Step 6: Implement — `apply_status.py`**

Read the CURRENT `apply_delivery_status()` function. Change:
```python
    found = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if not found:
        found = await c.conversations.apply_message_delivery_status(status.wamid, status.status)
```
to capture and use the string result instead of a bool — the routing logic (try messages only if outbound didn't match) stays the same, just check `result != "not_found"` instead of `not found`. This task does NOT yet wire in retry-triggering (that's Task 5) — just adapt the existing found/not-found routing and the existing `failed`-logging branch to the new return type, keep behavior otherwise identical.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/ tests/test_whatsapp_webhook.py -k "apply_outbound_delivery_status or apply_message_delivery_status or status" -v`

- [ ] **Step 8: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 9: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/app/core/apply_status.py backend/tests/
git commit -m "refactor(store): widen apply_*_delivery_status to distinguish a genuinely new status from a duplicate/regressive one"
```

---

### Task 2: `outbound_messages` retry-info + retry-record store methods

**Files:**
- Modify: `backend/app/store/base.py` (new `OutboundRetryInfo` dataclass, `IngestStore` protocol methods)
- Modify: `backend/app/store/postgres.py`
- Modify: `backend/app/store/memory.py` (`_OutboundRow` gains `retry_count`)
- Test: wherever `apply_outbound_delivery_status`/`mark_outbound_sent` are tested

**Interfaces:**
- Produces: `OutboundRetryInfo` dataclass (`id: int`, `phone_e164: str`, `payload_json: str`, `retry_count: int`); `IngestStore.get_outbound_retry_info(wamid: str) -> OutboundRetryInfo | None`; `IngestStore.record_outbound_retry(id: int, new_wamid: str | None) -> None`. Consumed by Task 4 (resend logic).

- [ ] **Step 1: Write the failing tests**

```python
async def test_get_outbound_retry_info_returns_current_state(store) -> None:
    # ... seed a row with a known wamid, phone, payload via this file's existing helper ...
    info = await store.get_outbound_retry_info("wamid.RETRY1")
    assert info is not None
    assert info.retry_count == 0
    assert info.phone_e164 == "<the seeded phone>"


async def test_get_outbound_retry_info_unknown_wamid_returns_none(store) -> None:
    info = await store.get_outbound_retry_info("wamid.NEVER_SEEN")
    assert info is None


async def test_record_outbound_retry_with_new_wamid_resets_delivery_status(store) -> None:
    # ... seed a row, apply "failed" delivery status ...
    await store.record_outbound_retry(row_id, "wamid.RESENT1")
    info = await store.get_outbound_retry_info("wamid.RESENT1")
    assert info is not None
    assert info.retry_count == 1
    entries = await store.find_outbound_by_phone(phone_e164)
    assert entries[-1].delivery_status is None  # reset, awaiting fresh confirmation


async def test_record_outbound_retry_with_no_new_wamid_increments_count_only(store) -> None:
    # ... seed a row ...
    await store.record_outbound_retry(row_id, None)
    entries = await store.find_outbound_by_phone(phone_e164)
    # delivery_status unchanged (still whatever it was, e.g. "failed"); retry_count incremented
    # (verify via get_outbound_retry_info on the ORIGINAL wamid, since no new one was assigned)
```
(Fill in the seeding mechanics using this test file's ACTUAL existing helper for creating a claimed-and-sent outbound row with a known wamid — read the file first, do not invent a new pattern.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/ -k "retry_info or record_outbound_retry" -v`

- [ ] **Step 3: Implement — `base.py`**

```python
@dataclass(frozen=True)
class OutboundRetryInfo:
    id: int
    phone_e164: str
    payload_json: str
    retry_count: int
```
Add to `IngestStore` Protocol:
```python
    async def get_outbound_retry_info(self, wamid: str) -> OutboundRetryInfo | None: ...

    # Records that a retry attempt was used. `new_wamid` non-None means the resend succeeded and
    # got a fresh wamid -- the row's wamid is updated to it and delivery_status reset to NULL (a
    # fresh send awaiting its own confirmation). `new_wamid` None means the resend attempt itself
    # could not be sent (or was suppressed by the kill switch) -- retry_count still increments,
    # but wamid/delivery_status are left as-is.
    async def record_outbound_retry(self, id: int, new_wamid: str | None) -> None: ...
```

- [ ] **Step 4: Implement — `postgres.py`**

```python
    async def get_outbound_retry_info(self, wamid: str) -> OutboundRetryInfo | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, phone_e164, payload_json, retry_count FROM outbound_messages"
                " WHERE template_wamid = $1",
                wamid,
            )
        if row is None:
            return None
        return OutboundRetryInfo(
            id=int(row["id"]),
            phone_e164=str(row["phone_e164"]),
            payload_json=str(row["payload_json"]),
            retry_count=int(row["retry_count"]),
        )

    async def record_outbound_retry(self, id: int, new_wamid: str | None) -> None:
        async with self._pool.acquire() as conn:
            if new_wamid is not None:
                await conn.execute(
                    "UPDATE outbound_messages SET retry_count = retry_count + 1,"
                    " template_wamid = $2, delivery_status = NULL, updated_at = now()"
                    " WHERE id = $1",
                    id, new_wamid,
                )
            else:
                await conn.execute(
                    "UPDATE outbound_messages SET retry_count = retry_count + 1, updated_at = now()"
                    " WHERE id = $1",
                    id,
                )
```

- [ ] **Step 5: Implement — `memory.py`**

Add `retry_count: int = 0` to `_OutboundRow` (the existing mutable per-row class). Implement `get_outbound_retry_info`/`record_outbound_retry` reading/writing that field directly, following the same lookup-by-wamid pattern already established for `apply_outbound_delivery_status`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/ -k "retry_info or record_outbound_retry" -v`

- [ ] **Step 7: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/tests/
git commit -m "feat(store): add retry-info and retry-record methods for outbound template sends"
```

---

### Task 3: `messages` retry-info + retry-record store methods

**Files:**
- Modify: `backend/app/store/base.py` (new `MessageRetryInfo` dataclass, `ConversationStore` protocol methods)
- Modify: `backend/app/store/postgres.py`
- Modify: `backend/app/store/memory.py`
- Test: wherever `apply_message_delivery_status`/`set_message_wamid` are tested

**Interfaces:**
- Produces: `MessageRetryInfo` dataclass (`id: int`, `conversation_id: int`, `content: str`, `retry_count: int`); `ConversationStore.get_message_retry_info(wamid: str) -> MessageRetryInfo | None`; `ConversationStore.record_message_retry(id: int, new_wamid: str | None) -> None`. Consumed by Task 4 (resend logic). This mirrors Task 2 exactly, for the `messages` table instead of `outbound_messages`.

- [ ] **Step 1: Write the failing tests**

Mirror Task 2's four tests exactly, against `get_message_retry_info`/`record_message_retry` and the `messages` table (use this test file's existing `append_message`/`set_message_wamid` helpers to seed a message with a known wamid).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/store/ -k "get_message_retry_info or record_message_retry" -v`

- [ ] **Step 3: Implement — `base.py`**

```python
@dataclass(frozen=True)
class MessageRetryInfo:
    id: int
    conversation_id: int
    content: str
    retry_count: int
```
Add to `ConversationStore` Protocol:
```python
    async def get_message_retry_info(self, wamid: str) -> MessageRetryInfo | None: ...

    async def record_message_retry(self, id: int, new_wamid: str | None) -> None: ...
```
(Same semantics as `record_outbound_retry` — see Task 2's docstring, mirror it.)

- [ ] **Step 4: Implement — `postgres.py`**

```python
    async def get_message_retry_info(self, wamid: str) -> MessageRetryInfo | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, conversation_id, content, retry_count FROM messages WHERE wamid = $1",
                wamid,
            )
        if row is None:
            return None
        return MessageRetryInfo(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            content=str(row["content"]),
            retry_count=int(row["retry_count"]),
        )

    async def record_message_retry(self, id: int, new_wamid: str | None) -> None:
        async with self._pool.acquire() as conn:
            if new_wamid is not None:
                await conn.execute(
                    "UPDATE messages SET retry_count = retry_count + 1,"
                    " wamid = $2, delivery_status = NULL WHERE id = $1",
                    id, new_wamid,
                )
            else:
                await conn.execute(
                    "UPDATE messages SET retry_count = retry_count + 1 WHERE id = $1", id
                )
```

- [ ] **Step 5: Implement — `memory.py`**

Read the CURRENT internal message-row representation first (introduced in the delivery-receipts feature's Task 2, mirroring `_OutboundRow`'s mutable-internal/frozen-view pattern). Add a `retry_count: int = 0` field to that internal row type, and implement `get_message_retry_info`/`record_message_retry` following the exact same shape as `set_message_wamid`/`apply_message_delivery_status` already do for wamid-based lookup.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/store/ -k "get_message_retry_info or record_message_retry" -v`

- [ ] **Step 7: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 8: Commit**

```bash
git add backend/app/store/base.py backend/app/store/postgres.py backend/app/store/memory.py backend/tests/
git commit -m "feat(store): add retry-info and retry-record methods for AI-reply messages"
```

---

### Task 4: Resend logic + owner alert (`app/core/delivery_retry.py`)

**Files:**
- Modify: `backend/app/jobs/outbox_drain.py` (make `_TemplatePayload`/`_parse_payload` public: rename to `TemplatePayload`/`parse_payload`, update this file's own internal call sites)
- Create: `backend/app/core/delivery_retry.py`
- Test: `backend/tests/core/test_delivery_retry.py` (new file)

**Interfaces:**
- Consumes: `OutboundRetryInfo`/`get_outbound_retry_info`/`record_outbound_retry` (Task 2); `MessageRetryInfo`/`get_message_retry_info`/`record_message_retry` (Task 3); `send_decision` (`app.core.send_policy`, already exists); `send_template`/`send_text`/`WhatsAppSendError` (`app.channels.whatsapp_sender`, already exist); `ConversationStore.get_user_id` (already exists, resolves `conversation_id` → phone for the AI-reply path).
- Produces: `MAX_RETRIES = 3` constant; `async def retry_failed_outbound(c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str) -> None`; `async def retry_failed_message(c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str) -> None`. Consumed by Task 5 (wiring).

**Why the rename in Step 1:** `_TemplatePayload`/`_parse_payload` are private (underscore-prefixed) to `outbox_drain.py` today, but this task's outbound-retry path needs the exact same payload-envelope parsing (template/language/body_params/buttons/image_url) to resend a template with its original content. Duplicating that parsing logic would violate DRY and risk drift between the two call sites; making it public and importing it is the correct fix (matching this project's own precedent of adjusting existing code when a genuine second consumer emerges, e.g. the delivery-receipts feature's `should_apply_delivery_status` extraction).

- [ ] **Step 1: Rename `_TemplatePayload`/`_parse_payload` to public names**

In `backend/app/jobs/outbox_drain.py`, rename `_TemplatePayload` → `TemplatePayload` and `_parse_payload` → `parse_payload`. Update every existing call site within this same file (there should be exactly one or two — read the file to find them). Run the existing test suite for this file to confirm nothing broke from the rename: `cd backend && python -m pytest tests/ -k outbox_drain -v`. Commit this as its own small step before continuing (keeps the rename separable from the new feature logic): `git add backend/app/jobs/outbox_drain.py && git commit -m "refactor(jobs): make TemplatePayload/parse_payload public for reuse by the delivery-retry feature"`.

- [ ] **Step 2: Write the failing tests**

Read `backend/app/jobs/outbox_drain.py`'s own tests for `send_one_outbound` first, to understand this codebase's established conventions for mocking `send_template`/`send_text` and asserting on `AdminControls`/`WhatsAppConfig` construction — match those conventions exactly rather than inventing new test scaffolding.

```python
async def test_retry_failed_outbound_resends_with_original_content(...) -> None:
    # Seed an outbound row (payload_json with a real template/params), mark it sent with a known
    # wamid, apply a "failed" delivery status. Mock send_template to return SendResult(ok=True,
    # wamid="wamid.RESENT", ...). Call retry_failed_outbound(c, wa_cfg, controls, "wamid.ORIGINAL").
    # Assert send_template was called with the SAME template/language/body_params/buttons/image_url
    # as the original payload. Assert the row's wamid is now "wamid.RESENT" and delivery_status is
    # None (via get_outbound_retry_info / find_outbound_by_phone).


async def test_retry_failed_outbound_stops_and_alerts_at_max_retries(...) -> None:
    # Seed a row already at retry_count == 3 (MAX_RETRIES). Call retry_failed_outbound. Assert
    # send_template was NOT called. Assert an alert text was sent to owner_alert_number (mock
    # send_text, assert it was called with owner_alert_number and a message mentioning the phone).


async def test_retry_failed_outbound_respects_send_mode_kill_switch(...) -> None:
    # controls.send_mode == "off" (or an allowlist miss). Call retry_failed_outbound. Assert
    # send_template was NOT called (suppressed by send_decision). Assert retry_count still
    # incremented (a "used" attempt) and the owner alert fired (since a suppressed resend can never
    # get a wamid to hang a future retry off of, per the design).


async def test_retry_failed_outbound_synchronous_send_failure_alerts_immediately(...) -> None:
    # Mock send_template to raise WhatsAppSendError. Call retry_failed_outbound with retry_count
    # still under MAX_RETRIES. Assert the owner alert still fires immediately (not waiting for
    # retry_count to reach 3) -- a send that can't even be attempted has no future wamid to retry.


async def test_retry_failed_outbound_bad_payload_alerts_immediately(...) -> None:
    # Seed a row with a corrupt/legacy payload_json (parse_payload returns None). Call
    # retry_failed_outbound. Assert send_template was NOT called, retry_count incremented, owner
    # alert fired.


async def test_retry_failed_message_resends_ai_reply_content(...) -> None:
    # Mirror the first test above for the messages/AI-reply path: seed a message with known
    # content + wamid via append_message/set_message_wamid, resolve recipient via the
    # conversation's user_id, mock send_text, assert it's called with the SAME content to the
    # SAME phone, assert wamid/delivery_status update the same way.


async def test_retry_failed_message_stops_and_alerts_at_max_retries(...) -> None:
    # Mirror the max-retries test for the messages path.


async def test_owner_alert_degrades_silently_when_unset(...) -> None:
    # controls.owner_alert_number == "" -- retry_failed_outbound/retry_failed_message at max
    # retries must not raise and must not attempt any send to an empty number.
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/core/test_delivery_retry.py -v`
Expected: FAIL — `app.core.delivery_retry` doesn't exist yet.

- [ ] **Step 4: Implement**

```python
# app/core/delivery_retry.py
import logging

from app.admin.controls import AdminControls
from app.channels.whatsapp_config import WhatsAppConfig
from app.channels.whatsapp_sender import WhatsAppSendError, send_template, send_text
from app.core.send_policy import send_decision
from app.deps import Container
from app.jobs.outbox_drain import parse_payload

logger = logging.getLogger("app.core.delivery_retry")

MAX_RETRIES = 3

_ALERT_TEMPLATE = (
    "Thetavas bot: a message to {phone} failed to deliver after {max_retries} retries and "
    "was not sent. You may want to follow up another way."
)


async def _alert_owner_retry_exhausted(
    c: Container, wa_cfg: WhatsAppConfig, owner_number: str, phone: str
) -> None:
    """Tell the store owner a message could not be delivered after every retry was used.

    Mirrors core/conversation.py::_alert_owner's shape (degrade silently if unset, never raise,
    log-only on a failed alert send) but with its own message -- this is a different situation
    (a delivery permanently failed) from that function's handoff-alert wording.
    """
    if not owner_number:
        return
    alert = _ALERT_TEMPLATE.format(phone=phone, max_retries=MAX_RETRIES)
    try:
        result = await send_text(c.http, wa_cfg, owner_number, alert)
        if not result.ok:
            logger.warning(
                "retry-exhausted owner alert failed: status=%s error=%s",
                result.status_code, result.error,
            )
    except WhatsAppSendError:
        logger.warning("retry-exhausted owner alert transport error")


async def retry_failed_outbound(
    c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str
) -> None:
    """Resend a template that just had a genuinely new 'failed' delivery status applied.

    Called only when apply_status.py's orchestrator confirms this wamid's failure was a fresh
    transition (not a duplicate/regressive report) -- see Task 1. No-ops if the wamid is unknown
    to outbound_messages (the caller already tried messages instead in that case).
    """
    info = await c.ingest.get_outbound_retry_info(wamid)
    if info is None:
        return
    if info.retry_count >= MAX_RETRIES:
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    payload = parse_payload(info.payload_json)
    if payload is None:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, info.phone_e164)
    if decision == "suppress":
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    try:
        result = await send_template(
            c.http, wa_cfg, info.phone_e164, payload.template, payload.language,
            payload.body_params, button_payloads=payload.buttons,
            header_image_url=payload.image_url,
        )
    except WhatsAppSendError:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)
        return

    if result.ok and result.wamid:
        await c.ingest.record_outbound_retry(info.id, result.wamid)
    else:
        await c.ingest.record_outbound_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, info.phone_e164)


async def retry_failed_message(
    c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, wamid: str
) -> None:
    """Resend an AI reply that just had a genuinely new 'failed' delivery status applied.

    Mirrors retry_failed_outbound exactly, for the messages/AI-reply table instead of
    outbound_messages -- see that function's docstring for the shared reasoning.
    """
    info = await c.conversations.get_message_retry_info(wamid)
    if info is None:
        return
    if info.retry_count >= MAX_RETRIES:
        phone = await c.conversations.get_user_id(info.conversation_id)
        await _alert_owner_retry_exhausted(
            c, wa_cfg, controls.owner_alert_number, phone or "unknown recipient"
        )
        return

    phone = await c.conversations.get_user_id(info.conversation_id)
    if phone is None:
        # Conversation row vanished (should not happen in practice) -- nothing sensible to retry.
        await c.conversations.record_message_retry(info.id, None)
        return

    decision = send_decision(controls.send_mode, controls.allowlist_phones, phone)
    if decision == "suppress":
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
        return

    try:
        result = await send_text(c.http, wa_cfg, phone, info.content)
    except WhatsAppSendError:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
        return

    if result.ok and result.wamid:
        await c.conversations.record_message_retry(info.id, result.wamid)
    else:
        await c.conversations.record_message_retry(info.id, None)
        await _alert_owner_retry_exhausted(c, wa_cfg, controls.owner_alert_number, phone)
```

Read `app/channels/whatsapp_config.py` for `WhatsAppConfig`'s exact import path and `app/admin/controls.py` for `AdminControls`'s exact import path before finalizing imports — confirm these match what's shown above (adapt if the actual module paths differ).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/core/test_delivery_retry.py -v`

- [ ] **Step 6: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/delivery_retry.py backend/tests/core/test_delivery_retry.py
git commit -m "feat(core): auto-retry a failed delivery up to 3 times, alert the owner on exhaustion"
```

---

### Task 5: Wire retry-triggering into the webhook handler

**Files:**
- Modify: `backend/app/core/apply_status.py` (`apply_delivery_status` — call the retry functions)
- Modify: `backend/app/channels/whatsapp.py` (`receive_webhook` — load `AdminControls`, pass through)
- Test: `backend/tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: `retry_failed_outbound`/`retry_failed_message` (Task 4); the widened `"not_found"|"applied"|"unchanged"` return values (Task 1).
- Produces: nothing consumed by a later task — this is the final task in the plan.

- [ ] **Step 1: Write the failing tests**

Read `backend/tests/test_whatsapp_webhook.py`'s existing status-wiring tests (from the delivery-receipts feature) fully first, to match its exact webhook-signing/seeding helpers.

```python
async def test_webhook_failed_status_triggers_a_retry_resend(client, monkeypatch, ...) -> None:
    # Seed an outbound row, sent, with a known wamid, retry_count 0. Mock send_template to
    # succeed with a new wamid. POST a signed webhook reporting "failed" for that wamid. Assert
    # 200. Assert the row's wamid changed and delivery_status is None (a retry fired).


async def test_webhook_duplicate_failed_status_does_not_retry_again(client, monkeypatch, ...) -> None:
    # Seed a row already delivery_status == "failed" with retry_count == 1 (a retry already
    # happened once). POST a signed webhook reporting "failed" AGAIN for the SAME wamid
    # (simulating Meta's redelivery). Assert send_template/send_text were NOT called again --
    # the "unchanged" result from Task 1 must prevent a second retry for the same failure.


async def test_webhook_retry_wiring_exception_still_acks_200(client, monkeypatch, ...) -> None:
    # Mock retry_failed_outbound (or send_template underneath it) to raise unexpectedly. POST a
    # signed webhook reporting "failed". Assert 200 -- matches the existing exception-swallowing
    # discipline already applied to the whole status-processing loop.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_whatsapp_webhook.py -k retry -v`

- [ ] **Step 3: Implement — `apply_status.py`**

Read the CURRENT `apply_delivery_status()` function (post-Task-1 changes). Widen its signature to accept `wa_cfg: WhatsAppConfig` and `controls: AdminControls`, and call the appropriate retry function when a `"failed"` status was genuinely newly applied:

```python
async def apply_delivery_status(
    c: Container, wa_cfg: WhatsAppConfig, controls: AdminControls, status: InboundStatus
) -> None:
    outbound_result = await c.ingest.apply_outbound_delivery_status(status.wamid, status.status)
    if outbound_result != "not_found":
        message_result = None
    else:
        message_result = await c.conversations.apply_message_delivery_status(
            status.wamid, status.status
        )

    if status.status == "failed":
        logger.warning("whatsapp delivery failed for wamid=%s", status.wamid)
        if outbound_result == "applied":
            await retry_failed_outbound(c, wa_cfg, controls, status.wamid)
        elif message_result == "applied":
            await retry_failed_message(c, wa_cfg, controls, status.wamid)
    elif outbound_result == "not_found" and message_result == "not_found":
        logger.debug(
            "delivery status update for unknown wamid=%s (status=%s)",
            status.wamid,
            status.status,
        )
```
(Adapt to the CURRENT actual structure of this function rather than assuming the above matches exactly — the shape of the found/not-found routing from Task 1 is what must be preserved; this snippet shows the intended final logic, not a literal diff.) Add the necessary imports (`retry_failed_outbound`, `retry_failed_message` from `app.core.delivery_retry`; `WhatsAppConfig`, `AdminControls` for the type hints).

- [ ] **Step 4: Implement — `whatsapp.py`**

Read the CURRENT `receive_webhook()` function fully (it already loads `cfg = await load_whatsapp_config(c.config)` — it does NOT currently load `AdminControls`). Add `controls = await load_controls(c.config)` (check `app/admin/controls.py` for `load_controls`'s exact import path) near where `cfg` is loaded, and update the status-processing loop's call to `apply_delivery_status(c, status)` to `apply_delivery_status(c, cfg, controls, status)`, keeping the existing try/except-per-status exception-swallowing wrapper exactly as-is (this task only widens the call's arguments, not its error-handling shape).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_whatsapp_webhook.py -k retry -v`

- [ ] **Step 6: Run the full backend suite + lint + mypy**

Run: `cd backend && python -m pytest -q && python -m ruff check . && python -m mypy app`

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/apply_status.py backend/app/channels/whatsapp.py backend/tests/test_whatsapp_webhook.py
git commit -m "feat(channels): wire delivery-failure auto-retry into the WhatsApp status webhook"
```

---

## Post-Implementation Notes

- No task touches `backend/app/core/order_actions.py` — verify empty diff before handoff to review.
- **Schema migration required before deploy** (see Global Constraints) — flag this explicitly at handoff, exactly as was done for the delivery-receipts feature. The owner must run the two `ALTER TABLE ... ADD COLUMN retry_count` statements before this code ships, or the new store methods (`get_outbound_retry_info`, `get_message_retry_info`, and both `record_*_retry` methods) will fail against production Postgres the first time a delivery-failure report arrives.
- Route to `code-reviewer` then `security-reviewer` after all 5 tasks land (this touches webhook-processing code and sends real messages), per this project's standard process for this class of change.
- Manual verification note: nothing in this feature can be exercised via a browser (it's a backend send/retry mechanism, not a UI change) — verification is via the automated test suite only, plus the owner observing real retry/alert behavior in production once deployed.
