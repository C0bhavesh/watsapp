# Phase 5 — Order Push + Confirm/Cancel Automation — Implementation Plan

> **For agentic workers:** implement task-by-task, TDD (RED→GREEN→commit). Spec:
> `docs/superpowers/specs/2026-08-10-phase5-order-push-design.md`. Grounded in ADR-001/002/004.

**Goal:** New Shopify order → WhatsApp Confirm/Cancel template (outbox drain); Confirm tap tags,
Cancel tap double-checks then cancels — the WATI-replacement outbound flow.

**Architecture:** Durable outbox drained by an authenticated cron job (ADR-001) gated by the
`send_mode` kill switch (ADR-002); deterministic button dispatch (NO LLM) that re-fetches live +
ownership-checks via `AuthorizedOrder` before any mutation (ADR-004); two-phase cancel.

## Global constraints
- Python 3.12+, full type hints, `mypy --strict` clean on `app`, `ruff check .` clean (line 100).
  Interpreter `python` = C:\Python313\python.exe; run from `backend/`.
- TDD every task: failing test → run → minimal code → green → commit. `pytest` asyncio_mode=auto.
- Secrets grep EMPTY on every touched `app/` file; no `print`; no bare `except`; no emojis in
  user-facing strings.
- **Mutation safety (ADR-004):** never construct `AuthorizedOrder` outside `core/order_resolver`;
  every button handler re-fetches live (`get_order`) and ownership-checks before tag/cancel.
- **The LLM is never in the button path.** Deterministic dispatch only.
- Never 5xx a signed webhook: button handler errors are caught, logged, degrade to a safe reply.
- Postgres tests: `@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), ...)`.
- Coordinate: another dev pushes to `main`; keep changes additive, do not refactor Phase 4 files
  beyond the one `whatsapp.py` wiring line and `copy.py` additions. NEVER push (Main Claude pushes).

**Existing interfaces (consume, do not change):** `ShopifyClient.get_order/add_tags(auth,tags)/
cancel_order(auth)->CancelRequested`; `Order.is_cancelled()/fulfillment_status/tags`;
`AuthorizedOrder(order,verified_phone)`; `resolve_by_phone/resolve_by_order_name` in
`core/order_resolver.py`; `send_template(...,button_payloads)` / `send_buttons(...,buttons)` /
`SendResult`; `InboundButton(payload,...)` / `InboundInteractive(button_id,...)`;
`load_controls(config)->AdminControls` (`send_mode, allowlist_phones, tags, default_language`);
`load_whatsapp_config(config)->WhatsAppConfig|None`; `normalize_phone`; `copy.copy_for(kind,lang)`;
`Container(shopify,ingest,config,http,messages,...)`; jobs registry in `app/jobs/router.py`.

---

### Task 1 — Store: outbox-drain + order_actions + mapping-status methods
**Files:** `app/store/base.py` (Protocol + `OutboundClaim` dataclass), `app/store/memory.py`,
`app/store/postgres.py`, `app/store/schema.sql` (only if a column is missing), tests
`tests/store/test_outbox_drain.py`.

**Produces** on `IngestStore` (both impls):
- `@dataclass(frozen=True) OutboundClaim: id:int; dedupe_key:str; phone_e164:str; payload_json:str; attempts:int`
- `claim_queued_outbound(limit:int=20) -> list[OutboundClaim]` — rows `state='queued'`, `ORDER BY created_at`, `LIMIT`.
- `mark_outbound_sent(id:int, wamid:str|None) -> None` — `state='sent', template_wamid=$, updated_at=now`.
- `mark_outbound_suppressed(id:int) -> None` — `state='suppressed'`.
- `mark_outbound_undeliverable(id:int, code:str) -> None` — `state='undeliverable', last_error_code=$`.
- `bump_outbound_attempt(id:int, code:str, max_attempts:int=5) -> str` — `attempts=attempts+1, last_error_code=$`; if new attempts ≥ max_attempts set `state='failed'` and return `'failed'`, else stay `'queued'` and return `'queued'`.
- `set_mapping_status(order_gid:str, status:str) -> None` — `order_mappings.status=$`.
- `record_order_action(order_gid:str, action:str, actor_wa_id:str|None, source_wamid:str|None, result:str, user_errors_json:str|None) -> None` — INSERT into `order_actions`.
- `orders_awaiting_cancel_reconcile(limit:int=50) -> list[str]` — `order_mappings.order_gid` where `status='cancel_requested'`.

All parameterized SQL. In-memory mirrors state transitions on the existing `self.outbound`/
`self.mappings` dicts + a new `self.order_actions: list`. Tests: claim returns only queued oldest-
first; each mark_* transitions state; bump reaches 'failed' at cap; set_mapping_status + record_
order_action persist; reconcile list filters by status. Postgres variants gated on TEST_DATABASE_URL.

Commit: `feat(store): outbox drain + order_actions + mapping-status methods`.

---

### Task 2 — Resolver: `resolve_by_gid`
**Files:** `app/core/order_resolver.py`, tests `tests/core/test_resolver_by_gid.py`.
**Produces:** `async resolve_by_gid(shopify:OrderSource, wa_id:str, gid:str) -> AuthorizedOrder|None`
— mirror `resolve_by_order_name`: `normalize_phone`; `order = await shopify.get_order(gid)` (add
`get_order` is already on `OrderSource`); on `ShopifyError`→None; `None`→None; wrap
`AuthorizedOrder(order, phone)` in try/except ValueError → None (non-owner → None, no enumeration).
Tests: owner gid → AuthorizedOrder; non-owner gid → None; missing → None; Shopify error → None;
bad wa_id → None. Commit: `feat(core): resolve_by_gid for deterministic button ownership`.

---

### Task 3 — Send-policy helper
**Files:** `app/core/send_policy.py`, tests `tests/core/test_send_policy.py`.
**Produces:** `send_decision(send_mode:str, allowlist:list[str], phone:str) -> str` returning
`"send"` | `"suppress"` (never raises): `off`→suppress; `shadow`→suppress; `allowlist`→ send iff
`normalize_phone(phone)` ∈ normalized allowlist else suppress; `live`→send; unknown mode→suppress
(fail safe). Tests each mode incl. allowlist hit/miss and unknown. (The drain treats `off`
specially — returns before looping — but this helper still maps off→suppress for safety.)
Commit: `feat(core): send-policy decision helper (kill switch)`.

---

### Task 4 — Outbox drain job
**Files:** `app/jobs/outbox_drain.py`, `app/jobs/router.py` (register), tests
`tests/test_outbox_drain_job.py`.
**Produces:** `async run_outbox_drain(c:Container) -> dict[str,object]`.
Behavior:
```
controls = await load_controls(c.config)
if controls.send_mode == "off": return {"drained":0,"sent":0,"suppressed":0,"reason":"send_mode off"}
cfg = await load_whatsapp_config(c.config)
if cfg is None: return {"drained":0,"error":"whatsapp not configured"}
rows = await c.ingest.claim_queued_outbound(limit=25)
sent=supp=failed=undeliv=0
for row in rows:
    gid = _gid_from_dedupe_key(row.dedupe_key)   # 'order_created:{gid}' -> gid; None -> mark_failed+continue
    if send_decision(controls.send_mode, controls.allowlist_phones, row.phone_e164) == "suppress":
        await c.ingest.mark_outbound_suppressed(row.id); supp+=1; continue
    payload = _parse_payload(row.payload_json)   # template, language, customer_name, order_name, amount; tolerate bad JSON -> mark_undeliverable
    r = await send_template(c.http, cfg, row.phone_e164, payload.template, payload.language,
            body_params=[payload.customer_name, payload.order_name, payload.amount],
            button_payloads=[f"order:confirm:{gid}", f"order:cancel:{gid}"])
    if r.ok:
        await c.ingest.mark_outbound_sent(row.id, r.wamid); await c.ingest.set_mapping_status(gid,"template_sent"); sent+=1
    elif r.status_code in _UNDELIVERABLE_CODES:   # {131026, 131047, 131049} recipient-not-reachable style
        await c.ingest.mark_outbound_undeliverable(row.id, str(r.status_code)); undeliv+=1
    else:
        state = await c.ingest.bump_outbound_attempt(row.id, str(r.status_code)); failed += (state=="failed")
return {"drained":len(rows),"sent":sent,"suppressed":supp,"failed":failed,"undeliverable":undeliv}
```
`WhatsAppSendError` (transport) around a row → bump_outbound_attempt, continue (never abort the
whole drain). Register `"outbox_drain": run_outbox_drain` in `_JOBS`. Body-param order MUST match
the `order_confirmation_cod` template (see `docs/whatsapp-templates.md`): confirm the placeholder
order before finalizing; if the template takes {name, order, amount} keep that order.
Tests: send_mode off → no send; live → send + mark_sent + status; shadow/allowlist-miss →
suppressed; undeliverable code → undeliverable; retryable code → attempt bumped; bad dedupe_key →
failed, others still processed; transport error → bumped, loop continues; button_payloads carry the
gid. Commit: `feat(jobs): outbox drain — send order template with confirm/cancel buttons`.

---

### Task 5 — Button dispatch (deterministic; the mutation-safety core)
**Files:** `app/core/order_actions.py`, tests `tests/core/test_button_dispatch.py`.
**Produces:** `async dispatch_button(c:Container, event:InboundButton|InboundInteractive) -> None`.
- Extract the payload string: `event.payload` (InboundButton) or `event.button_id` (InboundInteractive).
- Parse: must match `order:(confirm|cancel):{gid}` or `order:cancel:(confirm|abort):{gid}`; else →
  send generic safe reply (`copy_for("error_fallback", default_lang)`), return. (Order the regexes so
  `order:cancel:confirm:` / `order:cancel:abort:` match before bare `order:cancel:`.)
- `auth = await resolve_by_gid(c.shopify, event.wa_id, gid)`. If None → send refusal copy
  (`copy_for("not_found", lang)`), record NOTHING, return. **No order detail is ever sent for a gid
  the tapper does not own.**
- `lang = auth.order.customer_locale-derived (map to en/hi/gu) else controls.default_language`.
- Branch on action (all re-fetched state comes from `auth.order`, freshly fetched by resolve_by_gid):
  - **confirm:** cancelled → `copy_for("already_cancelled")`. confirmed-tag present → `copy_for("already_confirmed")`. else `add_tags(auth, controls.tags.confirmed)` → `record_order_action(gid,"confirm",wa_id,event.message_id,"ok",None)` → `set_mapping_status(gid,"confirmed")` → `copy_for("confirm_success")`.
  - **cancel (first tap):** cancelled → already_cancelled. `_is_dispatched(auth.order)` (fulfillment_status in {FULFILLED, PARTIALLY_FULFILLED, RESTOCKED, "fulfilled"...}) → `copy_for("cancel_too_late")` (contact support), NO mutation. else `send_buttons("Are you sure…", [(f"order:cancel:confirm:{gid}", yes_title),(f"order:cancel:abort:{gid}", no_title)])` (titles ≤20 chars, from copy). NO mutation.
  - **cancel:confirm:** cancelled → already_cancelled. dispatched now → cancel_too_late. else `cancel_order(auth)` → `add_tags(auth, controls.tags.cancel_requested)` (provisional) → `record_order_action(gid,"cancel_requested",...)` → `set_mapping_status(gid,"cancel_requested")` → `copy_for("cancel_requested")`. On `ShopifyGraphQLError` (userErrors) → `record_order_action(...,"error",json)` → `copy_for("cancel_failed")` (handoff).
  - **cancel:abort:** `copy_for("cancel_kept")`. No mutation.
- Wrap the whole body so a `ShopifyError`/unexpected exception → log + `copy_for("error_fallback")`
  send, never raise (caller must still ack 200).
- Idempotency: Meta message-id dedupe already happened at the webhook; the re-fetch state checks
  (already_confirmed/already_cancelled) stop a double mutation on a re-tap.
Add `AdminControls.tags.cancel_requested: list[str] = ["bot-cancel-requested"]` if absent (Task
extends `controls.py` TagLists minimally + a migration-safe default).
Tests (mocks; no real Shopify): non-owner gid → refusal, `add_tags`/`cancel_order` NOT called, no
order detail in the sent text; confirm tags + records action + status; re-tap confirm on a
confirmed order → "already confirmed", no second `add_tags`; cancel first tap → `send_buttons`
only, `cancel_order` NEVER called; fulfilled order cancel → cancel_too_late, no mutation;
cancel:confirm → `cancel_order` + provisional tag + status; abort → no mutation; foreign/garbage
payload → safe reply, no mutation; `orderCancel` userErrors → cancel_failed + audit; a raised
ShopifyError → error_fallback, no exception escapes. Commit: `feat(core): deterministic
confirm/cancel button dispatch with ownership + two-phase cancel`.

---

### Task 6 — Cancel reconciliation (final `cancelled` tag)
**Files:** `app/jobs/outbox_drain.py` (add `reconcile_cancels`) OR `app/jobs/retention.py`-style new
`app/jobs/reconcile.py`; register a job `reconcile_cancels`; tests `tests/test_reconcile_cancels.py`.
**Produces:** `async run_reconcile_cancels(c:Container) -> dict`: for each gid from
`orders_awaiting_cancel_reconcile(50)`: `o = await c.shopify.get_order(gid)`; if `o` and
`o.is_cancelled()` → need an `AuthorizedOrder` to tag: reconstruct via the mapping's phone
(`find_mappings_by_phone`? — simpler: add `add_tags` path). NOTE: `add_tags` needs `AuthorizedOrder`;
for a system reconciliation there is no tapper phone. Resolve by the order's own
`best_phone()` — construct `AuthorizedOrder(order=o, verified_phone=o.best_phone())` is valid (phone
matches the order by definition). Then `add_tags(auth, controls.tags.cancelled)` (final) →
`set_mapping_status(gid,"cancelled")` → `record_order_action(gid,"cancelled","system",None,"ok",None)`.
If not yet cancelled, leave in `cancel_requested` for the next run. (Construct the AuthorizedOrder
INSIDE order_resolver via a tiny `authorize_own_order(order)->AuthorizedOrder|None` helper so ADR-004
"only order_resolver constructs it" holds.)
Tests: a cancel_requested order now showing cancelledAt → final tag + status cancelled + audit; one
not yet cancelled → untouched; missing order → skipped. Commit: `feat(jobs): reconcile cancels —
final cancelled tag once Shopify confirms`.

---

### Task 7 — Wire button events into the webhook
**Files:** `app/channels/whatsapp.py`, tests `tests/test_whatsapp_webhook.py` (extend).
In the event loop, alongside the existing `InboundText` branch, add:
```
elif isinstance(event, (InboundButton, InboundInteractive)):
    if controls.send_mode != "off":
        await dispatch_button(c, event)
    # (deduped by MessageStore already; a tap is deterministic, not gated on paused_until)
```
Import `dispatch_button` from `app.core.order_actions`, `InboundButton/InboundInteractive` from the
inbound module. Keep the same budget/timeout guard the text path uses; a `dispatch_button`
exception must be swallowed so the webhook still returns 200 (mirror `run_turn`'s outer guard).
Tests: an `order:confirm:{gid}` webhook delivery → `dispatch_button` invoked, 200; button event when
`send_mode=off` → not dispatched, 200; handler raising → still 200. Commit: `feat(channels): wire
confirm/cancel button taps to deterministic dispatch`.

---

### Task 8 — Deterministic copy strings
**Files:** `app/channels/copy.py`, tests `tests/test_copy.py` (extend).
Ensure `copy_for(kind, lang)` covers all kinds used above in en/hi/hinglish/gu:
`confirm_success, already_confirmed, cancel_are_you_sure, cancel_yes_title (<=20),
cancel_no_title (<=20), cancel_requested, cancel_kept, cancel_too_late, already_cancelled,
cancel_failed, not_found, error_fallback`. Reuse existing keys where present; add missing with
warm, transactional, emoji-free wording (Hinglish served as its own variant). Tests: every kind
resolves non-empty for each language; button-title kinds ≤20 chars. Commit: `feat(copy): confirm/
cancel deterministic strings (4 languages)`.

---

### Task 9 — Registries, status, deferred list
**Files:** `docs/memory/component_registry.md`, `docs/memory/api_registry.md`,
`docs/FR/_pipeline_status.md`.
- component_registry: new modules (`core/order_actions.py`, `core/send_policy.py`, `jobs/
  outbox_drain.py`, `jobs/reconcile.py`, resolver `resolve_by_gid`, store methods).
- api_registry: `GET|POST /internal/jobs/outbox_drain` + `/internal/jobs/reconcile_cancels`
  (X-Cron-Secret); note button-tap handling on `POST /webhook/whatsapp`.
- pipeline_status: Phase 5 row → BUILT/REVIEW with test count; add a **"Phase 5 deferred / later"**
  block (self-invoke latency, SKIP LOCKED multi-instance drain, literal-YES free-text cancel,
  proactive shipped/cancelled topics) + carry the earlier deferred list (Postgres rate-limit,
  session revocation, public_base_url pinning, the 2 Phase-4 LOW cleanups, allowlist strict-isolation,
  secret rotation).
Commit: `docs(phase5): registries + pipeline status + deferred list`.

---

## Execution notes
- Order: 1 → 2 → 3 → 4, 5 (5 depends on 2,3; 4 depends on 1,3), 6 (dep 1,2), 7 (dep 5), 8 (used by
  5,7 — can precede 5), 9 last.
- After all tasks: `code-reviewer` (changed files) → `security-reviewer` (mutations/orderCancel/send
  path — MANDATORY) → fix findings TDD → re-verify. Main Claude pushes.
- Gates each task: `python -m pytest`, `ruff check .`, `mypy app`, secrets grep on touched files.
- Do NOT touch: HMAC verifiers, admin auth, Vertex/provider code, the DPDP/audit code, Phase 4
  agents/conversation beyond the single `whatsapp.py` wiring branch.
