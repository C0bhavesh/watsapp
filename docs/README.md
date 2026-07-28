# Docs Index — Shopify + WhatsApp Order Automation

Project: WhatsApp bot for a Shopify kurti store (thetavas) — customers ask about /
confirm / cancel / change address on their orders via WhatsApp; Gemini handles
free-text understanding; our backend talks to the Shopify Admin GraphQL API.

| File | What it contains |
|---|---|
| [raw-api-research.md](raw-api-research.md) | The original API research (req.md) preserved **as-is** — token grant + 5 Shopify API curls. |
| [shopify-connection-verification.md](shopify-connection-verification.md) | **Verdict: the connection approach is correct & officially supported.** Item-by-item verification against shopify.dev, caveats (API version, protected data, async cancel), and the customer_id fallback search. |
| [requirement-understanding.md](requirement-understanding.md) | What we're building: the two flows (user-initiated Q&A, business-initiated confirm/cancel), architecture, phone→order resolution strategy, tech stack direction. |
| [reference-project-ai-whatsapp-agent.md](reference-project-ai-whatsapp-agent.md) | Full exploration of D:\ai_whatsapp_agent (cafe bot) — what to copy (webhook/HMAC, sender, LLM JSON-intent engine, memory, secret vault) and what's missing (templates, Shopify side). |
| [open-questions.md](open-questions.md) | Round-1 questions — ANSWERED 2026-07-28, kept for history. |
| [theme-exploration-thetavas.md](theme-exploration-thetavas.md) | Client theme scan — no vendor traces in theme; **Shopflo checkout is live** (key constraint); cleanup notes. |
| [architecture-plan.md](architecture-plan.md) | **The architecture plan (v1.1)** — Levels 0–6 + accepted review amendments (outbox, kill switch, InboundButton, AuthorizedOrder…); gated 7-phase implementation plan. |
| [architecture-review-2026-07-28.md](architecture-review-2026-07-28.md) | Independent review report — SOUND WITH FIXES; 24 findings (F1–F24), confirmations, forward-compat matrix, ADR list. |
| [whatsapp-templates.md](whatsapp-templates.md) | Template drafts (UTILITY, en/hi/gu, Confirm/Cancel buttons) + API-creation JSON + design notes. |
| [phase0-verification-results.md](phase0-verification-results.md) | **Live API test results (2026-07-28)** — token/reads/mutation-schemas verified on 2026-07 against the real store; Shopflo order shape; `read_customers` scope gap. |
| [current-wati-bridge-analysis.md](current-wati-bridge-analysis.md) | Analysis of the owner's existing WATI bridge repo — how today's flow works, conventions we reuse, gaps we fix. |
| [FR/_pipeline_status.md](FR/_pipeline_status.md) | **Tier-1: current pipeline status** (cafe-project convention) — read every session. |
| [FR/client-decisions-all.md](FR/client-decisions-all.md) | All open client decisions, copy-paste ready to send. |
| [memory/error_learnings.md](memory/error_learnings.md) | Tier-1: mistakes solved + patterns — agents read before work, append after. |
| [memory/component_registry.md](memory/component_registry.md) | Tier-2: reusable components — grep before creating anything. |
| [memory/api_registry.md](memory/api_registry.md) | Tier-2: endpoints + external integrations — grep before adding routes. |
| [memory/observations.md](memory/observations.md) | Owner "save this in the memory folder" notes, numbered. |

**Status:** v1 scope locked (2026-07-28): orders/create webhook → template w/ Confirm/Cancel
buttons → tag/cancel; plus order-status Q&A (id+email+status) via Gemini. Meta Cloud API
direct; Vercel + Supabase. Next: design doc → plan → build.
