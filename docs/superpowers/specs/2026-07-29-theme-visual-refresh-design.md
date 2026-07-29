# TAVAS Theme Visual Refresh — Design

**Date:** 2026-07-29
**Status:** Approved
**Theme:** TAVAS Unpublished Draft — `#192459997552` only. Live theme ("Shopflo x Tavas | 05 June") and all other drafts/dev themes are explicitly untouched.

## Context

The draft theme is a minimal custom Shopify theme (not a marketplace theme). All global styling lives in one file, `theme/snippets/theme-styles.liquid` (577 lines), included via `theme/layout/theme.liquid`. There is no `assets/*.css` — the theme has no assets folder at all currently.

Current state:
- Palette already defined via theme settings: `--color-primary: #63182F` (wine), `--color-secondary: #8A5F5A` (mauve), `--color-tertiary: #B08D57` (gold), `--color-body-bg: #EDE2D0` (cream), `--color-body: #3D2F2A`.
- Headings use `Georgia, "Times New Roman", serif` (system font, no webfont loaded). Body uses `Inter, ui-sans-serif, system-ui...`.
- Buttons (`.button`) are already `border-radius: 999px` (pill) by default.
- Cards (`.category-card`, `.product-card`, `.collection-card`, `.policy-card`) share `border-radius: 18px`, `border`, `background: var(--color-surface)` — no hover states defined.
- Two responsive breakpoints exist: 989px (nav → drawer, grids → 2 col) and 699px (tighter section padding, grids → 2 col explicit).
- Brand positioning: "Old money minimal" — ethnic wear, affordable daily-wear pieces.

This is functional but visually generic — no distinctive type pairing, no hover/motion polish, boilerplate spacing. The goal is a refined "quiet luxury" pass, not a rebuild.

## Approach

Direction was validated via a visual mockup comparison (three hero treatments: Editorial Serif, Soft Centered Luxury, Bold Editorial Block). **Editorial Serif** was selected, with the further refinement of rounded (pill) buttons rather than sharp corners.

Locked visual language:
- Large serif display headings, tight line-height (~1.05).
- Hairline border frame accent on hero-type content blocks.
- Rounded pill buttons (already the base case; extend consistently).
- Existing palette unchanged.

## Scope

**In scope (visual-only, whole theme):**

1. **Typography** — Load a real serif display webfont (candidate: Fraunces or Cormorant — to be finalized in planning) for headings, replacing the Georgia fallback. Keep Inter for body/UI text. Load via Shopify's font picker / `{{ settings.type_header_font | font_face }}` pattern or a Google Fonts `<link>`, whichever fits the existing settings schema better.
2. **Buttons** — Confirm/normalize pill radius across `.button`, `.button--secondary`, `.button--ghost`. Add hover states (fill/border/shadow transition) — currently none exist.
3. **Cards** — `.category-card`, `.product-card`, `.collection-card`, `.policy-card`: add hover polish (image scale-up inside `__media`, shadow lift on the card, smooth transitions). Keep existing radius/border structure.
4. **Hero** (`sections/main-hero.liquid` + its styles) — apply the locked serif treatment and hairline frame accent per the approved mockup.
5. **Spacing/rhythm** — review `.section` padding, heading margins (`h1..h6`), and `.grid-cards` / `.product-grid` gaps for tighter, more intentional whitespace. Adjust values, not structure.
6. **Header/Footer** — polish nav link spacing and footer heading letter-spacing/tracking to match the new type scale. No structural changes.

**Out of scope:**
- Cart, checkout, or any JS/behavioral logic.
- New sections, blocks, or features.
- Any theme other than `#192459997552`.
- Product photography / image content changes.
- Automated visual regression tooling (none exists for this theme; manual review only).

## Files Likely Touched

- `theme/snippets/theme-styles.liquid` (primary — global styles, typography, buttons, cards, spacing)
- `theme/sections/main-hero.liquid` (hero markup/class adjustments if needed for the frame accent)
- `theme/sections/header.liquid`, `theme/sections/footer.liquid` (minor class/spacing only)
- `theme/config/settings_schema.json` (if a font picker setting is added)
- Possibly a new `theme/assets/` entry if a webfont needs local hosting (vs. Google Fonts link) — to be decided in planning based on Shopify font picker vs. external font.

## Testing / Verification

No automated test suite applies to Liquid/CSS. Verification is manual:
- `shopify theme dev` against the draft theme only, visual review of: homepage (hero, featured collection, category grid, value props), a product page, cart drawer/page, header/footer, and both responsive breakpoints (989px, 699px).
- Confirm no Liquid syntax errors (theme dev / `shopify theme check` if available).
- Confirm the live theme and other drafts remain untouched (no `theme push` to any theme ID other than `192459997552`, ever, without explicit confirmation).

## Risks / Notes

- Adding a webfont has a performance cost (extra request, FOUT/FOIT) — plan should specify `font-display: swap` and preconnect/preload to mitigate.
- This is a CLAUDE.md project for a *different* app (Thetavas Shopify × WhatsApp order bot backend); this theme work is unrelated to `backend/app/` and does not go through the `developer` Python agent — it's plain Liquid/CSS worked directly.
