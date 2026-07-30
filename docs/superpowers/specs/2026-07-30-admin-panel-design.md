# Admin Panel (Phase 3.5) — Design Spec

> Created 2026-07-30. Status: **APPROVED by owner 2026-07-30** (4 decisions below).
> Brainstormed 2026-07-29 (scope/access/UX) + 2026-07-30 (architecture, against the live
> reference project `D:\ai_whatsapp_agent` — present on this machine).
> Visual companion: architecture artifact "Thetavas Order Bot — Architecture & Admin Panel".

## Why this phase exists

The admin panel appeared in architecture-plan Level 3 (`app/admin/`) but never got a build
phase. Two facts made it a hard blocker:

1. **Critical Rule 1** — all Shopify/Meta/LLM credentials must be entered through a panel
   (Fernet-encrypted in `app_config`), never env vars. No panel = no production deploy.
2. **Client decision Q14 (2026-07-29)** — FAQ/policy content is delegated to us and the
   client edits it later, at runtime, without redeploys. That workflow *is* the panel.

A third bug is fixed here: the architecture described knowledge as shipped seed **files**,
but Vercel serverless files are read-only and reset on every deploy — client edits would
vanish. The cafe project solved this with a DB override table; we adopt it (Decision 2).

## Approved decisions (owner, 2026-07-30)

| # | Decision | Choice |
|---|---|---|
| 1 | How the panel is served | **JSON API under `/admin/*` + one static HTML/JS page mounted at `/admin/ui`** (StaticFiles, no build step, no new template deps — exact cafe pattern) |
| 2 | Where live knowledge lives | **New `knowledge_overrides` table + seed-file fallback + `knowledge_version` cache counter** (exact cafe pattern; supersedes the earlier `app_config` idea) |
| 3 | Admin password | **`ADMIN_PASSWORD` env var** — third env exception; CLAUDE.md Critical Rule 1 amended 2026-07-30 |
| 4 | Scope | **Full A–D**: credentials + knowledge editors + operational controls + read-only views |

Prior decisions carried in (2026-07-29 brainstorm): single shared password, full access, no
roles (door open for later); knowledge edited via **structured forms**, never raw JSON.

## A — Credentials (Fernet-encrypted via ConfigService; never echoed back)

| Group | Fields | Keys | Notes |
|---|---|---|---|
| Shopify *(new vs cafe)* | client_id, client_secret | `shopify:client_id`, `shopify:client_secret` (secrets) | Feed TokenManager + webhook HMAC. Existing keys — panel only writes them. |
| WhatsApp *(cafe)* | access_token, app_secret, verify_token (secret); phone_number_id, waba_id, api_version (plain) | `whatsapp:*` | First-time setup requires all six; afterwards each field updates independently (blank/omitted = keep stored value). |
| LLM provider *(cafe)* | provider choice + api_key | `provider_keys` table + active-provider marker | **Key live-verified with the provider before saving** (`verify_key`); failure → 400 with mapped message, nothing stored. RATE_LIMIT on an authenticated key → save + soft warning (cafe `accept_on_rate_limit`). |

## B — Knowledge (the bot's brain)

Four kinds, stored in `knowledge_overrides(kind PK, content, updated_at)`; shipped seed
files under `app/knowledge/seeds/` are fallback only. Every PUT bumps the
`knowledge_version` config counter so the Phase 4 assembler cache rebuilds on the next
message — **edits apply immediately, no redeploy**.

| Kind | Stored shape (cafe-compatible) | UI form |
|---|---|---|
| `brand_voice` | markdown | one textarea |
| `faq` | JSON list of `{q, a}` | add/edit/delete rows |
| `business` | JSON object | labeled fields |
| `patterns` | JSON list of `{pattern, examples[], reply}` | rows with example chips |

Server validates with Pydantic and serializes to exactly these shapes, so Phase 4 copies
the cafe `KnowledgeLoader`/`KnowledgeAssembler`/`KnowledgeCache` nearly verbatim and a
malformed save is structurally impossible. No `menu` kind — we do not sell in chat; order
facts always come live from Shopify.

Seed content: we draft Thetavas defaults (Q14) — delivery times, returns/exchange, COD
rules, damaged-item process, support contact, brand tone (warm, transactional, no emojis,
anti-prompt-injection rules mirroring the cafe brand_voice structure).

## C — Operational controls (plain `app_config` values, ADR-005)

| Key | Values | Purpose |
|---|---|---|
| `send_mode` | `off \| shadow \| allowlist \| live` | ADR-002 kill switch; default **off**; `shadow` = parallel-run vs WATI |
| `allowlist_phones` | JSON list of E.164 | safe live testing |
| `push_policy` | `cod_only \| all \| all_prepaid_no_buttons` | client chose `all` (Q1); reversible here |
| `reveal_fields` | subset of `[order_number, email, status]` | Q5 reveal set |
| `tags` | `{pending: [], confirmed: [], cancelled: []}` | Q13 dual-write lists |
| `default_language` | `en \| hi \| gu` | template language fallback |
| `push_staleness_hours` | int (default 6) | don't push stale orders |
| `public_base_url` | URL | webhook self-heal target |
| `owner_alert_number` | E.164 or empty | future handoff alerts (stored, unused v1) |

Every field server-validated (enums, E.164 regex, URL shape). GET returns current values
(these are not secrets).

## D — Read-only views

- `GET /admin/mappings?limit=` → recent `order_mappings` (order name, phone, status, timestamps).
- `GET /admin/outbox?limit=` → recent `outbound_messages` (state, attempts, last_error_code).
- Credential sections show only "configured" badges.

## API surface

| Endpoint | Auth | Behavior |
|---|---|---|
| `POST /admin/login` | rate-limited 5/min | password → signed cookie; 503 if `ADMIN_PASSWORD` unset; 401 on mismatch |
| `GET /admin/session` | cookie | cookie-only probe, **no DB access** |
| `GET/POST /admin/shopify` | cookie | badge status / save (partial-update semantics) |
| `GET/POST /admin/whatsapp` | cookie | non-secret status / save (first-time = all six required) |
| `GET /admin/providers`, `POST /admin/provider` | cookie | registry list / verify-then-save |
| `GET/PUT /admin/knowledge/{kind}` | cookie | override-else-seed / validate + save + version bump |
| `GET/PUT /admin/controls` | cookie | read / validate + save operational config |
| `GET /admin/mappings`, `GET /admin/outbox` | cookie | read-only, limit-capped (default 50, max 500) |
| `/admin/ui` | — | StaticFiles mount (login handled client-side) |

## Auth design (cafe `auth.py`, copied)

- Token: `<unix_exp>.<base64url HMAC-SHA256>` signed with `APP_MASTER_KEY`, 12h TTL.
- Cookie: `admin_session`, HttpOnly, SameSite=strict, path=/admin, `Secure` when
  `X-Forwarded-Proto: https` (Vercel terminates TLS).
- `check_password`: constant-time; empty configured password → always False (fail closed).
- `require_admin` FastAPI dependency on every route except login/static.
- New dependency: `slowapi` (login rate limit only — same lib as cafe).

## Data model change (additive)

```sql
CREATE TABLE IF NOT EXISTS knowledge_overrides (
  kind        TEXT PRIMARY KEY,
  content     TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- knowledge_version lives in app_config as an integer-valued row (bump on PUT)
```

`ConfigRepo` Protocol gains `get_knowledge_override(kind)`, `set_knowledge_override(kind,
content)`, `get_knowledge_overrides(kinds)`, `bump_config_int(key)` — implemented in both
in-memory and Postgres repos.

## Pulled forward from Phase 4 (shared, minimal)

- `app/knowledge/seeds/*` — four Thetavas seed files (content drafted by us).
- `KnowledgeLoader` (override-else-seed + version read) — needed for GET/PUT round trip.
- `app/providers/` minimal registry + `verify_key` (lazy `litellm` import per cold-start
  rule F10). Engine/assembler/cache/resolver/memory remain Phase 4.

## Security requirements

- All secrets Fernet-encrypted at rest; never logged; never returned to any UI.
- Secrets grep (no-secrets rule) must stay EMPTY on every new file.
- Login: fail closed on unset password; constant-time compares everywhere.
- Request body caps on admin POST/PUT (1 MiB, matching webhook posture).
- `security-reviewer` runs after `code-reviewer` (creds entry + auth + session surface).

## Testing requirements (TDD, RED→GREEN)

Token expiry/tamper/empty-secret · login rate limit + fail-closed paths · WhatsApp
partial-update semantics (blank keeps value; first-time requires all six) · secret non-echo
on every GET · knowledge shape validation (reject bad, accept good) + version bump ·
controls enum/E.164/URL validation · read-only view caps · static mount serves index.
Postgres impls gated on `TEST_DATABASE_URL` like the existing suite.

## Out of scope

Roles/multi-user · conversation engine · outbox drain · any Shopify/Meta behavior change ·
Vercel connection (still owner-deferred). Nothing here touches the webhook request paths.

## Reference files studied (cafe project)

`backend/app/admin/{router,auth}.py`, `backend/app/admin/static/{index.html,admin.js}`,
`backend/app/knowledge/{loader,assembler,cache}.py`, `backend/app/config/dynamic_config.py`,
`backend/app/store/{base,postgres_impl}.py` (knowledge_overrides), `schema.sql`,
`settings.py` (`admin_password`).
