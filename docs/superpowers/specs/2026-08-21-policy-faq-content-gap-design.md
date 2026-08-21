# Policy FAQ Content Gap — Design

> Owner-directed. Approved 2026-08-21.

## Problem

The owner supplied the store's 6 formal policy documents (`policy/Return Policy.docx`, `Exchange Policy.docx`, `Refund Policy.docx`, `Shipping Policy.docx`, `Privacy Policy.docx`, `Terms & Conditions.docx`) and asked that the bot be trained on them. Comparing their extracted text against the bot's current knowledge (`faq.json` + `business.json`'s `policy_notes`, the only two knowledge kinds `app/agents/policy.py` injects into its system prompt) shows most of the substance is already covered — color/print/embroidery variation disclaimers and the quality-inspection note are already in `policy_notes`, and Return/Exchange/Refund/Cancellation are already FAQ entries.

Two genuine gaps remain, both plausible customer questions the bot currently cannot answer from its knowledge and would fall back to the generic "not certain, email us" reply for:
1. **Courier delay disclaimer** — Terms & Conditions: not responsible for delays from courier partners, weather, festivals, strikes, government restrictions. Nothing in current knowledge addresses "why is my order late."
2. **Data sharing scope** — Privacy Policy: data may be shared with trusted payment gateways, courier partners, and service providers as necessary, or where legally required. Current `policy_notes` only states data is "used to process orders, provide support, and send order updates" and that it's not sold — incomplete versus the actual policy if a customer asks who sees their data.

## Scope

Content-only change to the two existing seed files already wired into `policy.py`'s system prompt. No new knowledge kind, no loader/admin-panel/router changes — `faq` and `business` are already fully admin-editable and already flow into the prompt via `{faq}`/`{business}` in `_SYSTEM_TEMPLATE`.

Explicitly out of scope (owner-confirmed): a "wrong shipping address" FAQ entry (Terms & Conditions' "customers must provide accurate shipping details" clause) — rarely asked proactively, not worth the entry. Also out of scope: the Terms & Conditions' internal-enforcement clauses ("fraudulent claims may be rejected," "TAVAS reserves the right to refuse service") — not something the bot should volunteer to customers, and the prompt's existing "answer ONLY from what's covered" instruction means omitting them just leaves the bot silent on that topic, which is correct.

## Content changes

`backend/app/knowledge/seeds/faq.json` — append two entries after the existing 8, same `{"q": ..., "a": ...}` shape and tone as the rest of the file:

- *"Why is my order delayed or taking longer than expected?"* → "Delivery can sometimes be affected by courier delays, weather, festivals, strikes, or government restrictions beyond our control. If your order is significantly past the estimated delivery window, message us with your order number and we'll look into it."
- *"Do you share my personal information with anyone?"* → "We never sell your personal information. We only share what's necessary with trusted payment gateways, courier partners, and service providers to process and deliver your order, or where required by law."

`backend/app/knowledge/seeds/business.json` — extend the existing `policy_notes` string, appending one sentence to the data-sharing clause: *"Personal information may also be shared with trusted payment gateways, courier partners, and service providers as necessary to fulfil an order or comply with legal requirements."*

## Testing

Mirrors the existing coverage pattern used for the COD FAQ correction earlier today: a `tests/knowledge/` assertion that the new FAQ entries exist with the expected key phrases (courier/weather/strikes present; payment gateways/courier partners present), and that `policy_notes` contains the data-sharing-with-partners phrase. No live-LLM test, consistent with this codebase's existing knowledge-grounding tests — automated tests verify the content reaches the prompt, not the AI's answer quality.

No admin-panel, router, or loader changes are needed or in scope; both files are already live-editable through the existing knowledge panel.
