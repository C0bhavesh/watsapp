# Error Learnings

> Append an entry whenever a non-obvious issue is encountered and solved, or when the human corrects a mistake. Agents read this before starting new work to avoid repeating known mistakes.

## Format
## [YYYY-MM-DD] Short Title
**Mistake/Issue:** what went wrong or was non-obvious
**Correct:** what should be done instead
**Pattern:** the general rule going forward

---
<!-- entries below -->

## [2026-07-28] Shopify orders are NOT searchable by phone
**Mistake/Issue:** `orders(query:"phone:...")` silently ignores the phone filter — it looks like it works but returns unfiltered results.
**Correct:** maintain our own `order_mappings` (phone_e164 → order_gid) filled from the `orders/create` webhook; fallback: `customers(query:"phone:...")` → `orders(query:"customer_id:...")`.
**Pattern:** verify every Shopify query filter against shopify.dev's supported-filter list before designing around it.

## [2026-07-28] Two different webhook HMAC schemes in one app
**Mistake/Issue:** Meta and Shopify both send HMAC-SHA256 signatures but encode them differently — copying the cafe verifier for Shopify would silently reject every webhook.
**Correct:** Meta `X-Hub-Signature-256` = **hex**; Shopify `X-Shopify-Hmac-Sha256` = **base64**. Two verifiers, both on the RAW body, constant-time compare.
**Pattern:** never reuse a signature verifier across providers without checking the encoding.

## [2026-07-28] Template quick-reply taps are message type "button", NOT interactive.button_reply
**Mistake/Issue:** A tap on a TEMPLATE quick-reply button arrives as `type:"button"` with `button.{text,payload}` + `context.id`; the cafe inbound parser only handles `type:"interactive"` (`interactive.button_reply`) and returns `None` for unknown types — so copying it wholesale makes every Confirm/Cancel tap fail SILENTLY (found by architecture review F4, before any code was written).
**Correct:** add an `InboundButton` variant to the typed union; template sends must attach an explicit per-button `payload` (≤128 chars, e.g. `order:confirm:{gid}`) or Meta returns only the language-dependent button text.
**Pattern:** when copying a channel parser across a message CLASS (template vs interactive), re-verify the inbound webhook shape against the provider's reference — not just the outbound payload.

## [2026-07-28] Vercel Python has no reliable "process after response" — use a durable outbox
**Mistake/Issue:** The plan said "ack Shopify 2xx <5s, process after" — but FastAPI BackgroundTasks on Vercel's buffered Python runtime run INSIDE the request cycle (delaying the response), and post-response work can be killed on instance reclaim. Combined with webhook-id idempotency, a lost send is never retried: customer silently gets no message (review F1).
**Correct:** webhook handler = one short DB transaction (dedupe insert + mapping upsert + `outbound_messages` queue row) → 200. A separate cron/self-invoked drain sends from the outbox with retries; `dedupe_key` UNIQUE makes "one push per order ever" a DB invariant.
**Pattern:** on serverless, anything that must survive the request needs a durable queue/table — never in-process background work.
