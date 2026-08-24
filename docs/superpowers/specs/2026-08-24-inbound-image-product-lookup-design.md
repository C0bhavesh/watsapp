# Inbound image product lookup — WhatsApp bot reads and displays customer photos

**Date:** 2026-08-24
**Status:** Approved (owner is sole decision-maker on this project — no separate client sign-off)

## Problem

Today the bot is completely blind to images. `channels/whatsapp_inbound.py::_parse_message` only
recognizes `text`, `button`, and `interactive` message types — an inbound WhatsApp image is silently
dropped (no acknowledgment, nothing stored, nothing visible anywhere). This is a known, documented
gap: `component_registry.md` already notes exchange/damage claims are routed straight to a human
"because checking those needs photo/video proof this bot cannot yet collect."

The requested use case here is **not** exchange/damage claims (explicitly out of scope for this
feature, per owner). It's product discovery: a customer sends a photo (e.g. a screenshot from
Instagram, a picture of an item they saw) and asks something like "what's the price of this," "do
you have this in size M," or "where do I buy this." The bot needs to (1) actually look at the image
(vision) and (2) ground its answer in the real Shopify catalog (price/size/availability/link), the
same no-hallucination posture `agents/product_search.py` already enforces for text-only questions.
Separately, the admin operator currently has no way to see an image a customer sent at all — the
admin chat page (`chats.js`) only renders text.

## Chosen approach

Insert image handling entirely at the **channel boundary**, synthesizing a plain-text message from
the image and feeding it into the **existing, unchanged** `core.conversation.run_turn()` pipeline —
intent classification, `product_search`'s grounded Shopify lookup, and reply generation all stay
exactly as they are today. This was chosen over extending the shared `LLMProvider`/`Message`
contract (used by every agent) to be multimodal: nothing else in this app needs vision, so widening
that shared interface would ripple into every agent for a capability only this one new code path
uses. `core/` and `agents/` do not need to know images exist.

Flow for an inbound image:
1. Meta's webhook delivers `{"type": "image", "image": {"id": "<media_id>", "mime_type": "...", "caption": "..."}}` — a media ID, not the image bytes.
2. Resolve the media ID via Meta's Graph API to a short-lived download URL, then fetch the bytes (both calls Bearer-authenticated with the existing `WhatsAppConfig.access_token`).
3. Enforce a size cap and mime-type allowlist on the downloaded bytes (see Safety below).
4. Persist the bytes (see Storage below), linked to the message row that will represent this turn.
5. Call Gemini (vision-capable, already the active model) with a fixed instruction to describe the product: item type, color, pattern, visible text/price. One short text description comes back.
6. Synthesize one text string combining the customer's caption (if any) with that description, e.g. `"{caption}\n\n[Photo — appears to show: {description}]"` (or just the bracketed part if there's no caption).
7. Construct an `InboundText` from that synthesized string and call `run_turn()` exactly as for a real typed message — nothing downstream changes.

The synthesized text (not a placeholder like "[image]") is what's persisted as the message's
`content`, so a later turn's conversation history stays coherent — e.g. if the customer follows up
with "in blue?", the model still has the product description in context. The actual image bytes are
stored separately, purely for the one-time vision call and for admin display.

## Storage

New table, following this codebase's existing schema convention (`schema.sql` is the source-of-truth
DDL; nothing in the app runs migrations automatically — the owner applies them manually, same as
every other schema change in this project's history):

```sql
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

**Amended during planning (2026-08-24), before implementation:** the schema above replaces an
earlier draft that FK'd `inbound_images.message_id -> messages.id`. That draft turned out to be
unworkable — the image is downloaded/stored *before* `run_turn()` is called, but the corresponding
`messages` row (role="user", the synthesized text) is only created *inside* `run_turn()`, so no
message row exists yet at the point the image needs to be stored. The implemented design instead
keys `inbound_images` on `(phone_e164, wamid)` — decoupled from `messages`/`conversations` entirely,
with no FK and no ordering dependency. The admin thread view (`IngestStore.find_inbound_images_by_phone`)
joins by phone at *read* time instead, mirroring the existing pattern already used there for
`template_sent`/`button_tap` entries (each a separate source merged into the thread by timestamp,
not a foreign key into `messages`). Stored in the existing Supabase Postgres database (owner-chosen
— no new infrastructure/credentials, consistent with this project's current all-Postgres stack).
The existing DPDP erasure (`delete_by_phone`) and retention purge (`purge_older_than`) are both
extended to also remove `inbound_images` rows, in the same "conversation history" retention class as
`conversations`/`messages`/`pending_actions`/`order_actions` (non-zero for both operations, unlike
the order/customer-mirror fields which are `delete_by_phone`-only per the existing Q15 decision).

## Provider layer

**Amended during planning (2026-08-24):** implemented as a new method on the shared `LLMProvider`
Protocol (`app/providers/base.py`), not concrete-only as originally drafted below. Reason: the
resolution helper `app.deps.active_llm()` returns the `LLMProvider` Protocol type, not the concrete
`LiteLLMProvider`; calling `.describe_image(...)` on that Protocol-typed value requires the method to
be declared on the Protocol to stay mypy-strict-clean without a cast/isinstance check. Adding one
orthogonal method to the Protocol does not force every existing agent to implement or use it (only
`Message.content` staying string-only — the thing that WOULD have rippled into every agent — was the
real minimal-blast-radius boundary, and that boundary is unchanged either way). Original rationale,
for context:

A new method on the concrete `LiteLLMProvider` (not added to the shared `LLMProvider` Protocol —
there is only one implementation today and nothing else needs to be swappable for this one narrow
capability; YAGNI):

```python
async def describe_image(
    self, image_bytes: bytes, mime_type: str, api_key: str, model: str, timeout: float
) -> str: ...
```

Reuses the same Gemini/Vertex auth branching and error classification `complete()` already has
(same `_classify`/`_redact` helpers), but sends an OpenAI-vision-shaped message
(`[{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:{mime_type};base64,..."}}]`)
via `litellm.acompletion` instead of the plain-string `messages` shape `complete()` builds. Called
directly by the new channel-boundary image-handling code — not through the agent/provider registry
machinery every text turn goes through, since this is a one-off preprocessing call, not a
conversation turn.

## Admin display

New endpoint, same `require_admin` session-cookie pattern as every other `/admin/*` route:
`GET /admin/conversations/{thread_id}/images/{id}` — streams the stored bytes back with their
`mime_type`. Needed because Meta's own media URL expires in minutes and isn't admin-authenticated,
so the admin page can't just link to it directly. `chats.js`'s message-rendering renders an `<img>`
for any message that has an associated image (fetched via this endpoint) instead of, or alongside,
its (synthesized) text.

## Safety

- **Size cap:** WhatsApp images can be up to ~5 MB; enforce that cap on the downloaded bytes before
  storing or sending to the vision model — reject (skip storage, skip vision call, fall back to a
  generic "I couldn't process that image" reply) anything larger.
- **Mime-type allowlist:** `image/jpeg`, `image/png`, `image/webp` — WhatsApp's own supported inbound
  image types. Anything else is rejected the same way as an oversized image.
- **SSRF posture:** mirrors the existing `error_learnings.md` [2026-08-15] "template header image URL
  is an SSRF-adjacent sink" lesson — even though the download URL comes from Meta's own Graph API
  response (not directly from attacker-controlled webhook input), validate its host is an expected
  Meta-owned domain before fetching, as defense in depth.
- **Never raises:** every step (media resolution, download, vision call) degrades to "skip this
  image, still process any caption as plain text if present, otherwise reply with a generic
  couldn't-process message" on any failure — matching `whatsapp_inbound.py`'s existing "attacker-typed
  input, never raise" posture for the whole parsing layer.
- **Secrets:** the Meta access token used for both Graph API calls is never logged; error messages
  from the media-fetch calls get the same token-redaction treatment `whatsapp_sender.py::_safe_error`
  already applies to send failures.

## Out of scope

- Exchange/damage-claim photo evidence (explicitly excluded by the owner — stays routed to human
  handoff exactly as today; unaffected by this feature).
- The bot/AI sending images to the customer mid-conversation (only the existing template
  header-image feature sends images today; that's unchanged).
- General-purpose vision beyond product lookup (e.g. reading an arbitrary document/screenshot for
  unrelated purposes) — the vision instruction is fixed and product-description-shaped; whatever
  the model returns still flows through the existing intent classifier like any other text, so a
  wildly off-topic image just gets whatever reply the existing agents would give the resulting text,
  same as today's behavior for an off-topic typed message.
- Video/audio/document/sticker/location inbound message types — still silently dropped, unchanged.
- Widening the shared `LLMProvider`/`Message` contract to be multimodal generally (see Provider layer
  above for why).

## Testing

Standard pattern for this codebase: pytest + pytest-asyncio, mocked `httpx` responses for the two new
Meta Graph API calls (media resolution + download) and the vision provider call, following the same
fake/mock conventions already used in `tests/channels/` and `tests/providers/`. The admin endpoint
and `chats.js` changes follow the existing admin test patterns (`tests/admin/test_views.py` for the
endpoint, `tests/admin/test_static_mount.py`'s substring-presence pattern for the JS, matching the
established, accepted limitation that this file has no browser/JS test runner).
