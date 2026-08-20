# Bot Size-Chart Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the WhatsApp bot a size chart to answer customer measurement questions from, editable in the admin panel, with S correctly marked as not currently sold.

**Architecture:** One new knowledge kind, `size_chart`, added to the existing admin-panel knowledge system (`brand_voice`/`faq`/`business`/`patterns`). New `SizeChartRow`/`SizeChartBody` Pydantic models validate admin PUTs; a new seed file ships the real chart data; the `product_search` subagent's system prompt gains a size-chart section; a new admin-panel tab lets the owner edit it going forward. The admin router's `/admin/knowledge/{kind}` route is already fully generic over `kind` — no router route changes needed.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, pytest + pytest-asyncio, vanilla JS admin panel.

## Global Constraints

- Full type hints; `mypy app` strict must stay clean.
- `ruff check .` must stay clean.
- Pydantic v2 models for all new request/response shapes, matching the existing `BusinessBody`/`FaqBody`/`PatternsBody` style in `backend/app/admin/knowledge_models.py` (named fields, explicit `max_length` caps on every string field, bounded list lengths).
- Only sizes marked `available: true` may have their measurements quoted to a customer as purchasable; a size that is `available: false` (S) or absent from the chart must be answered as "not currently available," never with its measurements presented as if purchasable. This is a prompt-instruction requirement (Task 2), not a code-enforced one — the model follows the instruction, same trust model as every other knowledge-grounded instruction in this codebase (e.g. policy.py's "never invent a policy detail").
- No schema/DB migration — this reuses the existing `knowledge_overrides` table and `ConfigRepo` used by every other knowledge kind.

---

### Task 1: Size-chart data model, seed, and knowledge-loader wiring

**Files:**
- Modify: `backend/app/admin/knowledge_models.py`
- Modify: `backend/app/knowledge/loader.py`
- Create: `backend/app/knowledge/seeds/size_chart.json`
- Test: `backend/tests/knowledge/test_loader.py`
- Test: `backend/tests/admin/test_knowledge_endpoints.py`

**Interfaces:**
- Produces: `SizeChartRow` (fields: `size: str`, `bust: str`, `waist: str`, `hip: str`, `kurta_length: str`, `pant_waist: str`, `pant_length: str`, `available: bool = True`), `SizeChartBody` (fields: `unit: str = "inches"`, `rows: list[SizeChartRow]`, `note: str = ""`) in `knowledge_models.py`. `validate_and_serialize("size_chart", payload)` returns the canonical JSON string. `KINDS` includes `"size_chart"`. Task 2 and Task 3 both consume `"size_chart"` as a valid kind string and the JSON shape these models serialize to (a dict with keys `unit`, `rows` (list of dicts with the 8 row keys above), `note`).

- [ ] **Step 1: Write the failing validation tests**

Add to `backend/tests/admin/test_knowledge_endpoints.py`, after `test_put_patterns_and_business_validate` (~line 65-67):

```python
def test_put_size_chart_validates(client: TestClient) -> None:
    login(client)
    ok = {
        "unit": "inches",
        "rows": [
            {"size": "M", "bust": "38", "waist": "36", "hip": "40", "kurta_length": "44",
             "pant_waist": "30-32", "pant_length": "38", "available": True},
        ],
        "note": "Size up if between sizes.",
    }
    assert client.put("/admin/knowledge/size_chart", json=ok).status_code == 200
    stored = json.loads(client.get("/admin/knowledge/size_chart").json()["content"])
    assert stored["rows"][0]["size"] == "M"
    assert stored["rows"][0]["available"] is True


def test_put_size_chart_rejects_empty_rows(client: TestClient) -> None:
    login(client)
    bad = {"unit": "inches", "rows": [], "note": ""}
    assert client.put("/admin/knowledge/size_chart", json=bad).status_code == 422


def test_put_size_chart_row_defaults_available_true(client: TestClient) -> None:
    login(client)
    # A row that omits "available" entirely defaults to sellable -- an admin adding a new size
    # row without touching the checkbox should not accidentally hide it from customers.
    payload = {
        "unit": "inches",
        "rows": [{"size": "L", "bust": "40", "waist": "38", "hip": "42",
                   "kurta_length": "44", "pant_waist": "32-34", "pant_length": "38"}],
        "note": "",
    }
    assert client.put("/admin/knowledge/size_chart", json=payload).status_code == 200
    stored = json.loads(client.get("/admin/knowledge/size_chart").json()["content"])
    assert stored["rows"][0]["available"] is True
```

Add `import json` at the top of this test file if it isn't already imported (check the file's existing imports first — other tests in this file already use `json.loads`, so it is very likely already imported; do not add a duplicate).

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/`): `python -m pytest tests/admin/test_knowledge_endpoints.py -k size_chart -v`
Expected: FAIL — `400` (unknown kind) since `"size_chart"` is not yet in `KINDS`.

- [ ] **Step 3: Add the Pydantic models**

In `backend/app/admin/knowledge_models.py`, add after `BusinessBody`'s `_cap_extra_entry_lengths` validator (right before the `def _dump` function):

```python
class SizeChartRow(BaseModel):
    size: str = Field(min_length=1, max_length=20)
    bust: str = Field(default="", max_length=50)
    waist: str = Field(default="", max_length=50)
    hip: str = Field(default="", max_length=50)
    kurta_length: str = Field(default="", max_length=50)
    pant_waist: str = Field(default="", max_length=50)
    pant_length: str = Field(default="", max_length=50)
    # Whether this size is currently sold. Lives on the row (not a separate list) so it can
    # never drift out of sync with the chart -- ticking/unticking one row's checkbox in the
    # admin panel is the only place this is ever set. Defaults True: an admin adding a new row
    # without touching the checkbox should not accidentally hide a size from customers.
    available: bool = True


class SizeChartBody(BaseModel):
    unit: str = Field(default="inches", max_length=20)
    rows: list[SizeChartRow] = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=2000)
```

Then extend `validate_and_serialize` (currently ends with `raise KeyError(kind)`):

```python
    if kind == "business":
        return _dump(BusinessBody.model_validate(payload).model_dump())
    if kind == "size_chart":
        return _dump(SizeChartBody.model_validate(payload).model_dump())
    raise KeyError(kind)  # guarded by the router's kind check before this call
```

- [ ] **Step 4: Create the seed file with the real chart data**

Create `backend/app/knowledge/seeds/size_chart.json`:

```json
{
  "unit": "inches",
  "rows": [
    {"size": "S", "bust": "36", "waist": "34", "hip": "38", "kurta_length": "44", "pant_waist": "28-30", "pant_length": "38", "available": false},
    {"size": "M", "bust": "38", "waist": "36", "hip": "40", "kurta_length": "44", "pant_waist": "30-32", "pant_length": "38", "available": true},
    {"size": "L", "bust": "40", "waist": "38", "hip": "42", "kurta_length": "44", "pant_waist": "32-34", "pant_length": "38", "available": true},
    {"size": "XL", "bust": "42", "waist": "40", "hip": "44", "kurta_length": "44", "pant_waist": "34-36", "pant_length": "38", "available": true},
    {"size": "XXL", "bust": "44", "waist": "42", "hip": "46", "kurta_length": "44", "pant_waist": "36-38", "pant_length": "38", "available": true}
  ],
  "note": "If you are between sizes, we recommend sizing up for a comfortable fit. S is not currently available for purchase."
}
```

- [ ] **Step 5: Wire the new kind into the knowledge loader**

In `backend/app/knowledge/loader.py`, change:

```python
KINDS: tuple[str, ...] = ("brand_voice", "faq", "business", "patterns")

_SEED_FILES: dict[str, str] = {
    "brand_voice": "brand_voice.md",
    "faq": "faq.json",
    "business": "business.json",
    "patterns": "patterns.json",
}
```

to:

```python
KINDS: tuple[str, ...] = ("brand_voice", "faq", "business", "patterns", "size_chart")

_SEED_FILES: dict[str, str] = {
    "brand_voice": "brand_voice.md",
    "faq": "faq.json",
    "business": "business.json",
    "patterns": "patterns.json",
    "size_chart": "size_chart.json",
}
```

- [ ] **Step 6: Update the seed-files-all-parse test**

In `backend/tests/knowledge/test_loader.py`, change:

```python
def test_all_seed_files_parse() -> None:
    for name in ("faq.json", "business.json", "patterns.json"):
        json.loads((SEEDS_DIR / name).read_text(encoding="utf-8"))
    assert (SEEDS_DIR / "brand_voice.md").read_text(encoding="utf-8").strip()
```

to:

```python
def test_all_seed_files_parse() -> None:
    for name in ("faq.json", "business.json", "patterns.json", "size_chart.json"):
        json.loads((SEEDS_DIR / name).read_text(encoding="utf-8"))
    assert (SEEDS_DIR / "brand_voice.md").read_text(encoding="utf-8").strip()
```

`test_assemble_all_covers_all_kinds` (same file) needs NO change — it already iterates `KINDS` generically (`set(parts) == set(KINDS)`), so it automatically covers the new kind once Step 5 lands.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/admin/test_knowledge_endpoints.py tests/knowledge/test_loader.py -v`
Expected: all pass, including the 3 new tests and the updated `test_all_seed_files_parse`.

- [ ] **Step 8: Run the full backend suite + mypy + ruff**

Run: `python -m pytest`
Expected: all pass (no regressions — `test_unknown_kind_400` in the same file uses `"menu"` as its deliberately-unknown kind, unaffected by adding `"size_chart"`).
Run (from `backend/`): `python -m mypy app/admin/knowledge_models.py app/knowledge/loader.py`
Run: `python -m ruff check app/admin/knowledge_models.py app/knowledge/loader.py backend/app/knowledge/seeds/size_chart.json backend/tests/admin/test_knowledge_endpoints.py backend/tests/knowledge/test_loader.py`
Expected: both clean. (ruff will simply skip the `.json` file — safe to include, no-op.)

- [ ] **Step 9: Secrets-compliance grep + `order_actions.py` check**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/admin/knowledge_models.py backend/app/knowledge/loader.py`
Expected: empty.
Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 10: Commit**

```bash
git add backend/app/admin/knowledge_models.py backend/app/knowledge/loader.py backend/app/knowledge/seeds/size_chart.json backend/tests/admin/test_knowledge_endpoints.py backend/tests/knowledge/test_loader.py
git commit -m "feat(knowledge): add size_chart knowledge kind with real chart data"
```

---

### Task 2: Ground the `product_search` subagent in the size chart

**Files:**
- Modify: `backend/app/agents/product_search.py`
- Test: `backend/tests/agents/test_product_search.py`

**Interfaces:**
- Consumes: `context.knowledge["size_chart"]` — a JSON string in the shape Task 1's `SizeChartBody.model_dump()` serializes to (a dict with `unit`, `rows`, `note`). This task does not parse that JSON; it interpolates the raw string into the prompt, exactly like `policy.py` already does for `context.knowledge.get("faq", "")`.
- Produces: nothing new consumed by a later task (Task 3 is independent, admin-panel-only).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/agents/test_product_search.py`, after `test_product_data_rendered_in_system_prompt` (ends ~line 159):

```python
async def test_size_chart_rendered_in_system_prompt() -> None:
    """Verify core computation: size-chart knowledge is grounded in the system prompt."""
    shopify = _FakeShopify(products=[])
    provider = _FixedProvider('{"reply": "Size M measurements: ..."}')
    context = AgentContext(
        wa_id="919999999999", phone_e164="+919999999999", user_text="what size is M",
        history=[], orders=[], is_vip=False,
        knowledge={"size_chart": '{"unit": "inches", "rows": [{"size": "M", "bust": "38", '
                                  '"available": true}], "note": "Size up if between sizes."}'},
        provider=provider, model="m", api_key="k", extra_params=None,
    )
    await run(context, shopify)

    assert len(provider.captured_messages) >= 1
    system_message = provider.captured_messages[0]
    assert system_message.role == "system"
    assert '"size": "M"' in system_message.content
    assert '"bust": "38"' in system_message.content
    assert "Size up if between sizes." in system_message.content
```

Read the `AgentContext` dataclass field order/types first at `backend/app/agents/base.py` (around line 70-90) before writing this — if it has grown additional required fields since this plan was written (e.g. a `timeout` field), add them to the constructor call above with a sensible literal value, matching how the file's OTHER existing tests already construct `AgentContext` (check `_context()` at the top of this same test file for the current full field list — this step's snippet may be missing a field that `_context()` already includes; reconcile against that helper, it is more likely to be current than this plan).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agents/test_product_search.py -k size_chart -v`
Expected: FAIL — the assertions on `'"size": "M"'`/`'"bust": "38"'`/the note text are not found in the system prompt (the current prompt template has no size-chart section).

- [ ] **Step 3: Extend the system prompt template**

In `backend/app/agents/product_search.py`, change:

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers find products. Below are REAL search results from the store's current
catalog -- you may ONLY describe products listed here. Never invent a product, price, color,
or availability that is not in this list.

{results_context}

If nothing suitable is listed above, say so honestly and offer to connect the customer with
the team, or suggest they describe what they're looking for a little differently.

{contract}
"""
```

to:

```python
_SYSTEM_TEMPLATE = """{personality}

You help customers find products. Below are REAL search results from the store's current
catalog -- you may ONLY describe products listed here. Never invent a product, price, color,
or availability that is not in this list.

{results_context}

If a customer asks about sizing or measurements, answer using ONLY the size chart below --
never guess or invent a measurement. Each row has an "available" flag: only quote measurements
for a row where "available" is true. If a customer asks about a size where "available" is
false, or a size that is not in this chart at all, tell them that size is not currently
available for purchase -- do not give its measurements as if it were purchasable. If you are
genuinely uncertain, say so and offer to connect them with the team.

Size chart:
{size_chart}

If nothing suitable is listed above, say so honestly and offer to connect the customer with
the team, or suggest they describe what they're looking for a little differently.

{contract}
"""
```

Then change the `run()` function's prompt-building call:

```python
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        results_context=_results_context(products),
        contract=HANDOFF_JSON_CONTRACT,
    )
```

to:

```python
    system_prompt = _SYSTEM_TEMPLATE.format(
        personality=personality_for(context),
        results_context=_results_context(products),
        size_chart=context.knowledge.get("size_chart", ""),
        contract=HANDOFF_JSON_CONTRACT,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/agents/test_product_search.py -k size_chart -v`
Expected: PASS

- [ ] **Step 5: Run the full product_search test file + full backend suite**

Run: `python -m pytest tests/agents/test_product_search.py -v`
Expected: all pass, including every pre-existing test (`_context()`'s hardcoded `knowledge={}` means `context.knowledge.get("size_chart", "")` renders as an empty string for those — confirm none of them assert on the exact system-prompt text in a way an empty `{size_chart}` interpolation would break; if one does, that is a real finding to report, not something to silently patch around).
Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 6: Run mypy + ruff**

Run (from `backend/`): `python -m mypy app/agents/product_search.py`
Run: `python -m ruff check app/agents/product_search.py backend/tests/agents/test_product_search.py`
Expected: both clean.

- [ ] **Step 7: Secrets-compliance grep + `order_actions.py` check**

Run: `grep -nE "sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|shpat_[A-Za-z0-9]{16,}|shpss_[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{40,}|api[_-]?key\s*=\s*['\"][^'\"]+" backend/app/agents/product_search.py`
Expected: empty.
Run: `git diff -- backend/app/core/order_actions.py`
Expected: empty output.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agents/product_search.py backend/tests/agents/test_product_search.py
git commit -m "feat(agents): ground product_search replies in the size chart"
```

---

### Task 3: Admin panel — Size Chart editor tab

**Files:**
- Modify: `backend/app/admin/static/index.html`
- Modify: `backend/app/admin/static/admin.js`

**Interfaces:**
- Consumes: `GET`/`PUT /admin/knowledge/size_chart` (Task 1), returning/accepting the `SizeChartBody` shape (`unit`, `rows` (each with `size`/`bust`/`waist`/`hip`/`kurta_length`/`pant_waist`/`pant_length`/`available`), `note`).
- Produces: nothing consumed by another task (leaf task, frontend-only).

- [ ] **Step 1: Add the CSS row class for an 8-column size row**

In `backend/app/admin/static/index.html`, in the existing `<style>` block, immediately after the `.row3` rule (~line 38):

```css
    .row3 { display: grid; grid-template-columns: 1fr 2fr 2fr auto; gap: .5rem; margin-bottom: .5rem; }
```

add:

```css
    .row-size { display: grid; grid-template-columns: .6fr 1fr 1fr 1fr 1fr 1fr 1fr auto auto;
      gap: .4rem; margin-bottom: .5rem; align-items: center; }
    .row-size label { font-size: .78rem; display: flex; align-items: center; gap: .25rem;
      white-space: nowrap; }
```

- [ ] **Step 2: Add the tab button and pane**

In `backend/app/admin/static/index.html`, change:

```html
      <div class="tabs">
        <button data-kind="brand_voice" class="active">Brand voice</button>
        <button data-kind="faq">FAQ</button>
        <button data-kind="business">Business</button>
        <button data-kind="patterns">Patterns</button>
      </div>
```

to:

```html
      <div class="tabs">
        <button data-kind="brand_voice" class="active">Brand voice</button>
        <button data-kind="faq">FAQ</button>
        <button data-kind="business">Business</button>
        <button data-kind="patterns">Patterns</button>
        <button data-kind="size_chart">Size chart</button>
      </div>
```

Then add a new pane immediately after the existing `pane-patterns` div closes (right before the `</div>` that closes `knowledge-card`, i.e. after the line `</div>` that follows `<div class="status" id="pat-status"></div>`):

```html
      <div class="kn-pane" id="pane-size_chart">
        <div class="field"><label for="sc-unit">Unit</label><input type="text" id="sc-unit" /></div>
        <div id="sc-rows"></div>
        <button class="small" id="sc-add">Add size</button>
        <div class="field" style="margin-top:.8rem"><label for="sc-note">Note</label>
          <input type="text" id="sc-note" /></div>
        <div style="margin-top:.8rem"><button id="sc-save">Save size chart</button></div>
        <div class="status" id="sc-status"></div>
      </div>
```

- [ ] **Step 3: Add the row-builder, load, and save logic**

In `backend/app/admin/static/admin.js`, immediately after the `addPatternRow`/`el("pat-add")` block (~line 177, right before the `const BIZ_FIELDS` line), add:

```javascript
function addSizeChartRow(row) {
  const r = row || {};
  const div = document.createElement("div");
  div.className = "row-size";
  const sizeI = document.createElement("input"); sizeI.placeholder = "Size"; sizeI.value = r.size || "";
  const bustI = document.createElement("input"); bustI.placeholder = "Bust"; bustI.value = r.bust || "";
  const waistI = document.createElement("input"); waistI.placeholder = "Waist"; waistI.value = r.waist || "";
  const hipI = document.createElement("input"); hipI.placeholder = "Hip"; hipI.value = r.hip || "";
  const klI = document.createElement("input"); klI.placeholder = "Kurta length"; klI.value = r.kurta_length || "";
  const pwI = document.createElement("input"); pwI.placeholder = "Pant waist"; pwI.value = r.pant_waist || "";
  const plI = document.createElement("input"); plI.placeholder = "Pant length"; plI.value = r.pant_length || "";
  const availLabel = document.createElement("label");
  const availI = document.createElement("input"); availI.type = "checkbox";
  availI.checked = r.available !== false; // default true, matches SizeChartRow's Pydantic default
  availLabel.appendChild(availI);
  availLabel.appendChild(document.createTextNode("Available"));
  div.appendChild(sizeI); div.appendChild(bustI); div.appendChild(waistI); div.appendChild(hipI);
  div.appendChild(klI); div.appendChild(pwI); div.appendChild(plI); div.appendChild(availLabel);
  div.appendChild(rowRemoveBtn(div));
  el("sc-rows").appendChild(div);
}
el("sc-add").addEventListener("click", () => addSizeChartRow(null));
```

Then extend `loadKnowledge` — change:

```javascript
async function loadKnowledge() {
  buildBizFields();
  const bv = await api("GET", "/admin/knowledge/brand_voice");
  el("bv-content").value = bv.content;
  const faq = JSON.parse((await api("GET", "/admin/knowledge/faq")).content);
  el("faq-rows").innerHTML = "";
  faq.forEach((it) => addFaqRow(it.q, it.a));
  const biz = JSON.parse((await api("GET", "/admin/knowledge/business")).content);
  for (const f of BIZ_FIELDS) el("biz-" + f).value = biz[f] || "";
  el("biz-note").value = biz.note || "";
  const pats = JSON.parse((await api("GET", "/admin/knowledge/patterns")).content);
  el("pat-rows").innerHTML = "";
  pats.forEach((p) => addPatternRow(p));
}
```

to:

```javascript
async function loadKnowledge() {
  buildBizFields();
  const bv = await api("GET", "/admin/knowledge/brand_voice");
  el("bv-content").value = bv.content;
  const faq = JSON.parse((await api("GET", "/admin/knowledge/faq")).content);
  el("faq-rows").innerHTML = "";
  faq.forEach((it) => addFaqRow(it.q, it.a));
  const biz = JSON.parse((await api("GET", "/admin/knowledge/business")).content);
  for (const f of BIZ_FIELDS) el("biz-" + f).value = biz[f] || "";
  el("biz-note").value = biz.note || "";
  const pats = JSON.parse((await api("GET", "/admin/knowledge/patterns")).content);
  el("pat-rows").innerHTML = "";
  pats.forEach((p) => addPatternRow(p));
  const sc = JSON.parse((await api("GET", "/admin/knowledge/size_chart")).content);
  el("sc-unit").value = sc.unit || "";
  el("sc-note").value = sc.note || "";
  el("sc-rows").innerHTML = "";
  (sc.rows || []).forEach((r) => addSizeChartRow(r));
}
```

Then add the save handler, immediately after the `el("pat-save")` block (~line 251, right before the `// ---- controls ----` comment):

```javascript
el("sc-save").addEventListener("click", async () => {
  const rows = [];
  el("sc-rows").querySelectorAll(".row-size").forEach((row) => {
    const inputs = row.querySelectorAll("input[type=text], input:not([type])");
    const [sizeI, bustI, waistI, hipI, klI, pwI, plI] = inputs;
    const availI = row.querySelector("input[type=checkbox]");
    if (sizeI.value.trim()) {
      rows.push({
        size: sizeI.value.trim(), bust: bustI.value.trim(), waist: waistI.value.trim(),
        hip: hipI.value.trim(), kurta_length: klI.value.trim(), pant_waist: pwI.value.trim(),
        pant_length: plI.value.trim(), available: availI.checked,
      });
    }
  });
  const body = { unit: el("sc-unit").value.trim(), rows, note: el("sc-note").value.trim() };
  try {
    await api("PUT", "/admin/knowledge/size_chart", body);
    setStatus("sc-status", "Saved " + rows.length + " sizes.", "ok");
  } catch (e) { setStatus("sc-status", e.message, "err"); }
});
```

The `inputs` selector `"input[type=text], input:not([type])"` deliberately excludes the checkbox (`type=checkbox`) — the 7 plain text inputs (created without an explicit `type` attribute, so their default type is `text`) are matched by `input:not([type])`, and the checkbox is excluded by both halves of the selector. Double-check this selector actually returns exactly 7 elements in the row's DOM order before trusting the destructuring — if `type=text]` vs `:not([type])` behaves unexpectedly in this codebase's target browsers, an equally correct alternative is `row.querySelectorAll("input")` filtered by `el => el.type !== "checkbox"` in plain JS; use whichever you've verified actually works when you test this in Step 4.

- [ ] **Step 4: Manual browser verification (owner-performed)**

This repo has no frontend test runner (documented, pre-existing gap) and most sandboxes have no browser — this step must be performed by the owner:
1. Open the admin settings page (`/admin/ui/index.html`), click the "Size chart" tab.
2. Confirm it loads pre-filled with the 5 seeded rows (S through XXL) and S's "Available" checkbox is UNCHECKED while M/L/XL/XXL are checked.
3. Edit a value (e.g. change M's bust measurement), click "Save size chart," confirm the "Saved 5 sizes" status message appears.
4. Reload the page, re-open the Size chart tab, confirm the edited value persisted.
5. Add a new row via "Add size," fill it in, save, confirm it persists on reload.
6. Remove a row via its "Remove" button, save, confirm the row count decreases and stays gone on reload.

- [ ] **Step 5: Commit**

```bash
git add backend/app/admin/static/index.html backend/app/admin/static/admin.js
git commit -m "feat(admin): add size chart editor tab to knowledge panel"
```

---

## Self-review notes (plan author)

- **Spec coverage:** `SizeChartRow`/`SizeChartBody` with `available` per-row (spec) ✓ Task 1 Step 3; seeded with real chart data, S `available=false` (spec) ✓ Task 1 Step 4; `KINDS`/loader wiring (spec) ✓ Task 1 Step 5; `product_search` grounding + "never quote an unavailable size's measurements" instruction (spec) ✓ Task 2 Step 3; admin panel table editor matching FAQ's UX (spec) ✓ Task 3.
- **Placeholder scan:** no TBD/TODO; every step has literal, complete code. Task 3 Step 3's selector caveat is a verification instruction, not a placeholder — the code itself is complete and functional as written, the note just tells the implementer what to double-check.
- **Type consistency:** `SizeChartRow`'s 8 field names (`size`/`bust`/`waist`/`hip`/`kurta_length`/`pant_waist`/`pant_length`/`available`) are identical across Task 1 (Pydantic model), Task 1's seed JSON, Task 2's test fixture JSON, and Task 3's JS row builder/save handler — verified matching in all four places.
- **Scope:** 3 tasks, each independently testable (backend model+seed, subagent prompt, admin UI) — matches the design spec's own section breakdown, no further decomposition needed.

## Next steps after all 3 tasks are done

1. Route to `code-reviewer` (scoped to all touched files across the 3 tasks).
2. This touches the AI prompt/knowledge-grounding path but no credentials, webhooks, mutations, auth, or CORS — per `.claude/rules/common/agents.md`, `security-reviewer` is not mandatory here (no sensitive surface in the routing table's sense), at the owner's discretion.
3. `doc-updater` updates `docs/memory/component_registry.md` (new `size_chart` kind, `KINDS` change) and `docs/memory/api_registry.md` (note the new `/admin/knowledge/size_chart` kind is covered by the already-documented generic route) + `docs/FR/_pipeline_status.md`.
4. No schema migration — nothing for the owner to run in Supabase.
5. Owner performs Task 3 Step 4's manual browser verification.
6. Owner reviews → push after approval (never auto-push, per CLAUDE.md Rule 7).
