# TAVAS Theme Visual Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved "Editorial Serif" visual refresh (real serif display webfont, rounded pill buttons with hover states, card hover polish, hero hairline frame accent, tightened spacing rhythm, header/footer polish) to the TAVAS Unpublished Draft theme only, with zero functional/behavioral changes.

**Architecture:** All changes live in one global stylesheet snippet, `theme/snippets/theme-styles.liquid`, plus one `<head>` addition in `theme/layout/theme.liquid` to load the webfonts. No new files, no JS, no section markup changes (the hero frame accent is done via CSS `::before`, not new HTML).

**Tech Stack:** Shopify Liquid, plain CSS (custom properties already in use via `:root`), Shopify CLI (`shopify theme dev` / `shopify theme pull`) for local preview against the draft theme.

## Global Constraints

- Target theme is `#192459997552` ("TAVAS Unpublished Draft") ONLY. Never run `shopify theme push` or `shopify theme dev` against any other theme ID.
- Visual-only changes. No cart/checkout/JS/behavioral changes. No new sections, blocks, or settings beyond what's specified below.
- Keep the existing palette exactly: `--color-primary: #63182F`, `--color-secondary: #8A5F5A`, `--color-tertiary: #B08D57`, `--color-body-bg: #EDE2D0`, `--color-body: #3D2F2A`.
- No automated test framework exists for Liquid/CSS in this repo. Every task's "test" step is a manual visual check via `shopify theme dev` against theme `192459997552`, described precisely enough to be unambiguous (what page, what to look for).
- Commit after every task with a `style:` prefixed conventional commit message (this is pure visual/styling work, not `feat`/`fix`).
- Working directory for all Liquid/CSS edits: `d:/bhvaesh_automation/theme/`.

---

### Task 1: Load webfonts and apply typography tokens

**Files:**
- Modify: `theme/layout/theme.liquid` (insert font `<link>` tags before line 17, `{% render 'theme-styles' %}`)
- Modify: `theme/snippets/theme-styles.liquid` (`:root` block, `body`, `h1..h6`, `.eyebrow`, `.header__name`)

**Interfaces:**
- Produces: CSS custom properties `--font-heading` and `--font-body`, consumed by Task 5 (`.section__heading`) and Task 6 (`.header__nav`, `.site-footer__heading`) implicitly via the `h1..h6` and `body` cascade, and directly by name where noted.

- [ ] **Step 1: Add Google Fonts preconnect + stylesheet link in `theme/layout/theme.liquid`**

Find this exact block (currently lines 14-18):

```liquid
  {%- if page_description -%}
    <meta name="description" content="{{ page_description | escape }}">
  {%- endif -%}
  {% render 'theme-styles' %}
  {{ content_for_header }}
```

Replace with:

```liquid
  {%- if page_description -%}
    <meta name="description" content="{{ page_description | escape }}">
  {%- endif -%}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300;0,400;0,500;0,600;1,400;1,500&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  {% render 'theme-styles' %}
  {{ content_for_header }}
```

Note: `display=swap` is already part of the Google Fonts URL, so text renders immediately in a fallback font and swaps in Fraunces/Inter when loaded — no separate `font-display` CSS declaration is needed since these are hosted (not `@font-face`-declared locally).

- [ ] **Step 2: Add font tokens to `:root` in `theme/snippets/theme-styles.liquid`**

Find this exact block (currently lines 6-17):

```css
:root {
  --color-primary: {{ settings.color_primary | default: '#63182F' }};
  --color-secondary: {{ settings.color_secondary | default: '#8A5F5A' }};
  --color-tertiary: {{ settings.color_tertiary | default: '#B08D57' }};
  --color-body-bg: {{ settings.color_body_bg | default: '#EDE2D0' }};
  --color-body: {{ settings.color_body_color | default: '#3D2F2A' }};
  --color-surface: #F7F1EA;
  --color-line: rgba(61, 47, 42, 0.14);
  --color-soft: rgba(138, 95, 90, 0.1);
  --radius: 8px;
  --container: min(1180px, calc(100vw - 2rem));
}
```

Replace with:

```css
:root {
  --color-primary: {{ settings.color_primary | default: '#63182F' }};
  --color-secondary: {{ settings.color_secondary | default: '#8A5F5A' }};
  --color-tertiary: {{ settings.color_tertiary | default: '#B08D57' }};
  --color-body-bg: {{ settings.color_body_bg | default: '#EDE2D0' }};
  --color-body: {{ settings.color_body_color | default: '#3D2F2A' }};
  --color-surface: #F7F1EA;
  --color-line: rgba(61, 47, 42, 0.14);
  --color-soft: rgba(138, 95, 90, 0.1);
  --radius: 8px;
  --container: min(1180px, calc(100vw - 2rem));
  --font-heading: 'Fraunces', Georgia, "Times New Roman", serif;
  --font-body: 'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
```

- [ ] **Step 3: Use the body font token**

Find this exact block (currently lines 27-33):

```css
body {
  margin: 0;
  background: var(--color-body-bg);
  color: var(--color-body);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.55;
}
```

Replace with:

```css
body {
  margin: 0;
  background: var(--color-body-bg);
  color: var(--color-body);
  font-family: var(--font-body);
  line-height: 1.55;
}
```

- [ ] **Step 4: Use the heading font token, tighten line-height and letter-spacing**

Find this exact block (currently lines 92-98):

```css
h1, h2, h3, h4, h5, h6 {
  margin: 0 0 0.75rem;
  color: var(--color-primary);
  line-height: 1.1;
  font-family: Georgia, "Times New Roman", serif;
  font-weight: 500;
}
```

Replace with:

```css
h1, h2, h3, h4, h5, h6 {
  margin: 0 0 0.85rem;
  color: var(--color-primary);
  line-height: 1.08;
  letter-spacing: -0.01em;
  font-family: var(--font-heading);
  font-weight: 500;
}
```

- [ ] **Step 5: Set the eyebrow to the heading (serif) font, matching the approved mockup**

Find this exact block (currently lines 84-90):

```css
.eyebrow {
  margin: 0 0 0.5rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 0.75rem;
  color: var(--color-secondary);
}
```

Replace with:

```css
.eyebrow {
  margin: 0 0 0.5rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 0.75rem;
  color: var(--color-secondary);
  font-family: var(--font-heading);
}
```

- [ ] **Step 6: Use the heading font token for the header brand name**

Find this exact block (currently lines 170-176):

```css
.header__name {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.25rem;
  color: var(--color-primary);
  letter-spacing: 0.04em;
  white-space: nowrap;
}
```

Replace with:

```css
.header__name {
  font-family: var(--font-heading);
  font-size: 1.25rem;
  color: var(--color-primary);
  letter-spacing: 0.04em;
  white-space: nowrap;
}
```

- [ ] **Step 7: Verify no other hardcoded `Georgia` references remain**

Run: `grep -rn "Georgia" theme/` (or use the Grep tool)
Expected: no matches (both prior occurrences were replaced in Steps 4 and 6).

- [ ] **Step 8: Manual visual verification**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

This starts a local preview server bound to the draft theme and prints a `localhost` preview URL. Open it and check:
- Homepage hero heading and store name in the header render in the serif Fraunces font (not the old Georgia system serif) — visually check letter shapes look like a designed typeface, not the OS default serif.
- Body text (paragraphs, nav links) renders in Inter, not a system sans fallback.
- No layout breakage from the font swap (headings shouldn't overflow their containers).

Stop the dev server (Ctrl+C) when done — do not leave it running unattended.

- [ ] **Step 9: Commit**

```bash
git add theme/layout/theme.liquid theme/snippets/theme-styles.liquid
git commit -m "style(theme): load Fraunces/Inter webfonts and apply heading/body font tokens"
```

---

### Task 2: Button hover states

**Files:**
- Modify: `theme/snippets/theme-styles.liquid` (`.button`, `.button--secondary`, `.button--ghost`)

**Interfaces:**
- Consumes: `--color-primary`, `--color-secondary`, `--color-line`, `--color-soft` (already defined in `:root`).
- Produces: none consumed by later tasks (self-contained).

- [ ] **Step 1: Add transitions and hover states to button classes**

Find this exact block:

```css
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 2.875rem;
  padding: 0.75rem 1.2rem;
  border-radius: 999px;
  border: 1px solid var(--color-primary);
  background: var(--color-primary);
  color: #fff;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.button--secondary {
  background: transparent;
  color: var(--color-primary);
}

.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border-color: var(--color-line);
}
```

Replace with:

```css
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 2.875rem;
  padding: 0.75rem 1.2rem;
  border-radius: 999px;
  border: 1px solid var(--color-primary);
  background: var(--color-primary);
  color: #fff;
  font-weight: 600;
  letter-spacing: 0.01em;
  transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
}

.button:hover {
  background: var(--color-secondary);
  border-color: var(--color-secondary);
  box-shadow: 0 10px 24px rgba(99, 24, 47, 0.22);
  transform: translateY(-1px);
}

.button--secondary {
  background: transparent;
  color: var(--color-primary);
}

.button--secondary:hover {
  background: var(--color-primary);
  color: #fff;
}

.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border-color: var(--color-line);
}

.button--ghost:hover {
  border-color: var(--color-primary);
  background: var(--color-soft);
}
```

- [ ] **Step 2: Manual visual verification**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

On the homepage hero, hover each button:
- Primary button ("Shop new arrivals"): background shifts from wine to mauve, lifts slightly, gains a soft shadow.
- Secondary button ("Browse under 2500"): fills with wine background and white text on hover.
- Confirm the hover transition is smooth (not instant/jarring) and buttons don't shift the surrounding layout.

- [ ] **Step 3: Commit**

```bash
git add theme/snippets/theme-styles.liquid
git commit -m "style(theme): add hover states and transitions to buttons"
```

---

### Task 3: Card hover polish (product, category, collection, policy cards)

**Files:**
- Modify: `theme/snippets/theme-styles.liquid` (`.category-card`/`.product-card`/`.collection-card`/`.policy-card` and their `__media` rules)

**Interfaces:**
- Consumes: `--color-line`, `--color-tertiary` (already defined in `:root`).
- Produces: none consumed by later tasks (self-contained).

- [ ] **Step 1: Add shadow-lift hover to the card shells**

Find this exact block:

```css
.category-card,
.product-card,
.collection-card,
.policy-card {
  display: block;
  overflow: hidden;
  border: 1px solid var(--color-line);
  border-radius: 18px;
  background: var(--color-surface);
}
```

Replace with:

```css
.category-card,
.product-card,
.collection-card,
.policy-card {
  display: block;
  overflow: hidden;
  border: 1px solid var(--color-line);
  border-radius: 18px;
  background: var(--color-surface);
  transition: box-shadow 0.25s ease, transform 0.25s ease, border-color 0.25s ease;
}

.category-card:hover,
.product-card:hover,
.collection-card:hover,
.policy-card:hover {
  box-shadow: 0 18px 35px rgba(61, 47, 42, 0.14);
  transform: translateY(-3px);
  border-color: var(--color-tertiary);
}
```

- [ ] **Step 2: Add image zoom-on-hover to card media**

Find this exact block:

```css
.category-card__media img,
.product-card__media img,
.collection-card__media img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
```

Replace with:

```css
.category-card__media img,
.product-card__media img,
.collection-card__media img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 0.4s ease;
}

.category-card:hover .category-card__media img,
.product-card:hover .product-card__media img,
.collection-card:hover .collection-card__media img {
  transform: scale(1.05);
}
```

- [ ] **Step 3: Manual visual verification**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

On the homepage (category grid, featured collection) and a collection page:
- Hover a product/category card: card lifts with a soft shadow, border tints gold, and the image inside zooms in slightly.
- Confirm the zoom stays clipped inside the card (no image overflow outside the rounded corners) — this relies on `overflow: hidden` already present on the card shell.

- [ ] **Step 4: Commit**

```bash
git add theme/snippets/theme-styles.liquid
git commit -m "style(theme): add hover polish to product/category/collection cards"
```

---

### Task 4: Hero hairline frame accent

**Files:**
- Modify: `theme/snippets/theme-styles.liquid` (`.hero__content`)

**Interfaces:**
- Consumes: none new.
- Produces: none consumed by later tasks (self-contained). No markup changes needed — `sections/main-hero.liquid` is untouched since the frame is a CSS `::before` pseudo-element.

- [ ] **Step 1: Add the inset hairline frame to the hero content block**

Find this exact block:

```css
.hero__content {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 28rem;
}
```

Replace with:

```css
.hero__content {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 28rem;
}

.hero__content::before {
  content: "";
  position: absolute;
  inset: 0.75rem;
  border: 1px solid rgba(99, 24, 47, 0.15);
  border-radius: 14px;
  pointer-events: none;
}
```

- [ ] **Step 2: Manual visual verification**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

On the homepage hero:
- Confirm a thin wine-tinted inset border frame is visible inside the hero content panel, offset from its outer edge — matching the approved "Editorial Serif" mockup.
- Confirm the frame doesn't intercept clicks (buttons and text inside the hero remain fully clickable — `pointer-events: none` on the frame handles this, but verify by clicking the primary button).

- [ ] **Step 3: Commit**

```bash
git add theme/snippets/theme-styles.liquid
git commit -m "style(theme): add hairline frame accent to hero content"
```

---

### Task 5: Spacing and grid rhythm

**Files:**
- Modify: `theme/snippets/theme-styles.liquid` (`.section`, `.section__heading`, `.grid-cards`, `.product-grid`, and the `699px` media query's `.section` override)

**Interfaces:**
- Consumes: none new.
- Produces: none consumed by later tasks (self-contained).

- [ ] **Step 1: Increase section padding for more breathing room**

Find this exact block:

```css
.section {
  padding: 3rem 0;
}

.section__heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1.25rem;
}
```

Replace with:

```css
.section {
  padding: 4rem 0;
}

.section__heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1.75rem;
}
```

- [ ] **Step 2: Widen grid gaps**

Find this exact block:

```css
.grid-cards {
  display: grid;
  gap: 1rem;
}
```

Replace with:

```css
.grid-cards {
  display: grid;
  gap: 1.5rem;
}
```

Find this exact block:

```css
.product-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
```

Replace with:

```css
.product-grid {
  display: grid;
  gap: 1.5rem;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
```

- [ ] **Step 3: Adjust the mobile section padding override to match the new baseline proportionally**

Find this exact block inside the `@media (max-width: 699px)` rule:

```css
  .section {
    padding: 2rem 0;
  }
```

Replace with:

```css
  .section {
    padding: 2.5rem 0;
  }
```

- [ ] **Step 4: Manual visual verification across breakpoints**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

Using browser devtools responsive mode, check the homepage at three widths:
- Desktop (>989px): sections have visibly more vertical breathing room than before; card grids (category grid, product grid) have wider gutters between cards.
- Tablet (~989px, just under): grids collapse to 2 columns (existing behavior, unaffected by this task) with the new wider gap.
- Mobile (~699px, just under): section padding is present but tighter than desktop, no cramped/overlapping content.

- [ ] **Step 5: Commit**

```bash
git add theme/snippets/theme-styles.liquid
git commit -m "style(theme): widen section padding and grid gaps for spacing rhythm"
```

---

### Task 6: Header nav and footer heading polish

**Files:**
- Modify: `theme/snippets/theme-styles.liquid` (`.header__nav`, `.header__nav a`, `.site-footer__heading`)

**Interfaces:**
- Consumes: `--font-heading` (from Task 1), `--color-primary`.
- Produces: none consumed by later tasks (self-contained).

- [ ] **Step 1: Add an animated underline hover to header nav links**

Find this exact block:

```css
.header__nav {
  display: flex;
  align-items: center;
  gap: 1.25rem;
}

.header__nav a {
  color: var(--color-body);
  font-size: 0.95rem;
}
```

Replace with:

```css
.header__nav {
  display: flex;
  align-items: center;
  gap: 1.75rem;
}

.header__nav a {
  position: relative;
  color: var(--color-body);
  font-size: 0.92rem;
  letter-spacing: 0.02em;
  transition: color 0.2s ease;
}

.header__nav a::after {
  content: "";
  position: absolute;
  left: 0;
  right: 100%;
  bottom: -4px;
  height: 1px;
  background: var(--color-primary);
  transition: right 0.25s ease;
}

.header__nav a:hover {
  color: var(--color-primary);
}

.header__nav a:hover::after {
  right: 0;
}
```

Note: `.header__nav a[aria-current="page"]` already sets `color: var(--color-primary)` in a separate rule further down the file — that rule is untouched and continues to apply on top of this one.

- [ ] **Step 2: Give footer headings the serif heading font and tighter tracking**

Find this exact block:

```css
.site-footer__heading {
  margin-bottom: 0.85rem;
  font-size: 1rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
}
```

Replace with:

```css
.site-footer__heading {
  margin-bottom: 1rem;
  font-size: 0.95rem;
  font-family: var(--font-heading);
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--color-primary);
}
```

- [ ] **Step 3: Manual visual verification**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

- Desktop header: hover each nav link, confirm a thin wine underline animates in from the left and the link text tints wine-colored; confirm the currently active page link (if any) still shows its static wine color from the existing `[aria-current="page"]` rule.
- Footer: confirm column headings ("Shop", "Help", etc.) now render in the serif heading font, in wine color, uppercase with wide tracking.
- Mobile (<989px): confirm the hamburger/drawer nav (`.header__drawer`) is unaffected — this task only touched `.header__nav`, the desktop nav list.

- [ ] **Step 4: Commit**

```bash
git add theme/snippets/theme-styles.liquid
git commit -m "style(theme): polish header nav hover and footer heading typography"
```

---

### Task 7: Full-theme review pass

**Files:** none modified — verification only. If this step surfaces a defect, fix it in the relevant file from Tasks 1-6 and re-run this task's checks before committing that fix.

**Interfaces:** none.

- [ ] **Step 1: Confirm no Liquid syntax errors**

Run: `shopify theme check theme/` (from `d:/bhvaesh_automation/`)
Expected: no errors related to files touched in Tasks 1-6 (`theme.liquid`, `theme-styles.liquid`). Pre-existing warnings unrelated to this work are not this plan's concern — do not fix unrelated theme-check findings as part of this pass.

- [ ] **Step 2: Full manual walkthrough against the draft theme**

Run: `shopify theme dev --theme=192459997552 --path="d:/bhvaesh_automation/theme"`

Walk through and visually confirm the combined result of Tasks 1-6 on:
- Homepage (`index.json`): hero, featured collection, category grid, value props.
- A product page (`product.json`): gallery, product form, price — confirm serif heading font on the product title, card/button hover states on any related-product cards.
- Cart page (`cart.liquid`): confirm button styling (Task 2) applies correctly to cart actions, no layout breakage.
- Header and footer on at least one non-homepage page.
- Repeat a quick pass at ~989px and ~699px widths (devtools responsive mode) to confirm nothing introduced in Tasks 1-6 breaks the existing responsive behavior.

- [ ] **Step 3: Confirm theme scope boundary**

Run: `git status` and `git diff --stat`
Expected: only `theme/layout/theme.liquid` and `theme/snippets/theme-styles.liquid` show changes across this plan's commits (check via `git log --stat` over the Task 1-6 commits). Confirm no `shopify theme push` was run against any theme ID other than `192459997552`, and no push was run at all without separate explicit approval (pushing is a deploy-adjacent action outside this plan's scope — `theme dev` syncs live-preview only to the draft theme, which is the approved target).

- [ ] **Step 4: Final commit (if Step 1/2 surfaced fixes)**

If no fixes were needed, skip this step — Task 7 is verification-only and produces no commit. If a fix was made:

```bash
git add theme/snippets/theme-styles.liquid theme/layout/theme.liquid
git commit -m "style(theme): fix issues found in full-theme review pass"
```
