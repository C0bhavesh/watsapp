# Bot Size-Chart Knowledge — Design

> Owner-directed. Approved 2026-08-19.

## Problem

Customers ask the WhatsApp bot for size/measurement info ("what's the measurement for size M"), and the bot has no data to answer from — it would either refuse or, worse, guess. The owner has a standard size chart (kurta set + pant, S–XXL, in inches) and wants the bot trained to answer from it. One important business rule: **only M–XXL are currently sold** — S is on the chart (for reference / future use) but not available for purchase, and the bot must say so rather than quoting S's measurements as if it were purchasable.

## Scope

One new knowledge kind, `size_chart`, added to the existing admin-panel knowledge system (`faq`/`business`/`patterns`/`brand_voice` today) — same edit-in-panel, takes-effect-immediately pattern, no redeploy needed for future size/measurement changes. Grounded into the `product_search` subagent, since the intent router already classifies "asking about a specific product/item/size/color" as `product_search` — no router changes needed.

Out of scope (per owner's confirmation): multiple charts per product category. This is the one store-wide chart for now; a future multi-chart design is a separate feature if/when the store adds size-differentiated categories.

## Data shape

`SizeChartRow` (new, `backend/app/admin/knowledge_models.py`, mirrors the existing `BusinessBody`'s named-field style — not a generic key-value blob):
- `size: str` (e.g. "S", "M", "XXL")
- `bust`, `waist`, `hip`, `kurta_length`, `pant_waist`, `pant_length: str` (free text, so a range cell like "28-30" works — not numeric, matching the chart's own "Pant Waist (Elastic)" column)
- `available: bool` (default `True`) — whether this size is currently sold. **S ships seeded as `False`**; M/L/XL/XXL ship `True`. This lives on the row (not a separate list) so it can never drift out of sync with the chart itself — when S becomes sellable, the owner just ticks its checkbox in the panel.

`SizeChartBody`:
- `unit: str` (default "inches")
- `rows: list[SizeChartRow]`
- `note: str` — free text for the chart's own footnote ("if you are between sizes, size up")

Stored/serialized the same way every other kind is (`validate_and_serialize` → canonical JSON string via `_dump`), and loaded the same way (`KnowledgeLoader.get("size_chart")` returns the stored override or the seed). No new storage mechanism.

## Seed content

`backend/app/knowledge/seeds/size_chart.json` ships pre-filled with the real chart from the owner's screenshot (S–XXL, S `available=false`, M–XXL `available=true`), matching `faq.json`'s existing precedent of shipping real Thetavas content, not a blank placeholder.

## Bot behavior (`product_search` subagent)

`backend/app/agents/product_search.py`'s system prompt gains a size-chart section, interpolating the raw stored JSON directly (same pattern `policy.py` already uses for `faq`/`business` — no custom text-table renderer needed, the LLM reads the JSON fine) plus explicit instructions:

- Answer sizing/measurement questions using ONLY this chart — never guess or invent a measurement (same "never invent" discipline the prompt already enforces for products).
- Only quote measurements for a row where `available` is `true`. For a size that is `available: false` (S today) or not present in the chart at all, tell the customer that size isn't currently available — do not give its measurements as if it were purchasable.
- If genuinely uncertain, say so and offer to connect them with the team (matches the existing fallback pattern in `policy.py`/`product_search.py`).

This is additive to the existing "only describe products in the search results below" instruction — that constraint is about product existence/price/availability from Shopify search results; the size-chart instructions are a separate, independent grounding block for a different kind of question the same subagent now also handles.

## Admin panel

New "Size Chart" section in the knowledge panel (`backend/app/admin/static/index.html` + `admin.js`), following the FAQ editor's existing UX exactly: a table with a header row (Size, Bust, Waist, Hip, Kurta Length, Pant Waist, Pant Length, Available) and one editable row per size, an "Add size" button (mirrors `addFaqRow`/`faq-add`), a Unit field and a Note textarea, and a Save button — `PUT /admin/knowledge/size_chart` (the router's `/admin/knowledge/{kind}` route is already fully generic over `kind`, so no router changes are needed beyond the new Pydantic model + its `validate_and_serialize` branch).

## Testing

Mirrors the existing coverage pattern for `business`/`faq`: `knowledge_models.py` validation tests (valid payload round-trips, missing/oversized fields rejected), admin router GET/PUT tests for the `size_chart` kind (auth-gated, validates, bumps `knowledge_version`), and a `product_search.py` test confirming the size-chart JSON is interpolated into the system prompt sent to the provider (mirrors how `policy.py`'s existing tests confirm `faq`/`business` show up in its prompt).

No live LLM test — same as every other knowledge-grounding feature in this codebase, the AI's actual answer quality is not something automated tests verify; this is architecture-level grounding, not behavior verification.
