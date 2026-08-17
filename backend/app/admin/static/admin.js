// Thetavas admin panel - vanilla JS, no build step. Cookies carry the session.
"use strict";

function el(id) { return document.getElementById(id); }

function setStatus(id, text, kind) {
  const node = el(id);
  node.textContent = text || "";
  node.className = "status" + (kind ? " " + kind : "");
}

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : res.status;
    throw new Error("Request failed: " + detail);
  }
  return data;
}

function showPanel() {
  el("login-screen").classList.remove("active");
  el("panel-screen").classList.add("active");
  loadAll();
}

// ---- login -----------------------------------------------------------------
el("login-btn").addEventListener("click", async () => {
  setStatus("login-status", "Logging in...");
  try {
    await api("POST", "/admin/login", { password: el("password").value });
    setStatus("login-status", "");
    showPanel();
  } catch (e) { setStatus("login-status", "Login failed. Check the password.", "err"); }
});
el("password").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("login-btn").click();
});

// ---- shopify ---------------------------------------------------------------
async function loadShopify() {
  const s = await api("GET", "/admin/shopify");
  const b = el("shopify-badge");
  const wss = s.webhook_signing_secret_configured ? " + webhook secret" : "";
  b.textContent = s.configured ? "configured" + wss : "not configured";
  b.className = "badge" + (s.configured ? " on" : "");
}
el("sh-save").addEventListener("click", async () => {
  try {
    await api("POST", "/admin/shopify", {
      client_id: el("sh-client-id").value || null,
      client_secret: el("sh-client-secret").value || null,
      webhook_signing_secret: el("sh-webhook-signing-secret").value || null,
    });
    el("sh-client-id").value = ""; el("sh-client-secret").value = "";
    el("sh-webhook-signing-secret").value = "";
    setStatus("sh-status", "Saved.", "ok");
    loadShopify();
  } catch (e) { setStatus("sh-status", e.message, "err"); }
});

// ---- whatsapp ----------------------------------------------------------------
const WA_FIELDS = ["phone_number_id", "waba_id", "api_version", "access_token", "app_secret", "verify_token"];
function waInputId(name) { return "wa-" + name.replace(/_/g, "-"); }
async function loadWhatsApp() {
  const s = await api("GET", "/admin/whatsapp");
  const b = el("wa-badge");
  b.textContent = s.configured ? "configured" : "not configured";
  b.className = "badge" + (s.configured ? " on" : "");
  if (s.phone_number_id) el("wa-phone-number-id").value = s.phone_number_id;
  if (s.waba_id) el("wa-waba-id").value = s.waba_id;
  if (s.api_version) el("wa-api-version").value = s.api_version;
}
el("wa-save").addEventListener("click", async () => {
  const body = {};
  for (const f of WA_FIELDS) {
    const v = el(waInputId(f)).value.trim();
    body[f] = v || null;
  }
  try {
    await api("POST", "/admin/whatsapp", body);
    for (const f of ["access_token", "app_secret", "verify_token"]) el(waInputId(f)).value = "";
    setStatus("wa-status", "Saved.", "ok");
    loadWhatsApp();
  } catch (e) { setStatus("wa-status", e.message, "err"); }
});

// ---- llm provider ------------------------------------------------------------
// provider key -> auth_kind ("api_key" | "env"). Env providers (Vertex) authenticate
// from server env credentials, so no API key is pasted or stored.
let providerAuthKinds = {};

async function loadProviders() {
  const list = await api("GET", "/admin/providers");
  const sel = el("llm-provider");
  sel.innerHTML = "";
  providerAuthKinds = {};
  for (const p of list) {
    providerAuthKinds[p.key] = p.auth_kind || "api_key";
    const opt = document.createElement("option");
    opt.value = p.key; opt.textContent = p.label + " (" + p.default_model + ")";
    sel.appendChild(opt);
  }
  applyProviderAuthUI();
  const cfg = await api("GET", "/admin/config");
  const b = el("llm-badge");
  b.textContent = cfg.configured ? "configured: " + cfg.provider : "not configured";
  b.className = "badge" + (cfg.configured ? " on" : "");
}

// Hide the API-key field and relabel the button for env-auth (Vertex) providers.
function applyProviderAuthUI() {
  const isEnv = providerAuthKinds[el("llm-provider").value] === "env";
  const keyField = el("llm-key-field");
  if (keyField) keyField.style.display = isEnv ? "none" : "";
  el("llm-save").textContent = isEnv ? "Use this provider" : "Verify and save";
}
el("llm-provider").addEventListener("change", applyProviderAuthUI);

el("llm-save").addEventListener("click", async () => {
  const provider = el("llm-provider").value;
  const isEnv = providerAuthKinds[provider] === "env";
  setStatus("llm-status", isEnv ? "Activating provider..." : "Verifying key with the provider...");
  try {
    const payload = isEnv ? { provider } : { provider, api_key: el("llm-key").value };
    const r = await api("POST", "/admin/provider", payload);
    el("llm-key").value = "";
    const ok = isEnv ? "Provider activated." : "Key verified and saved.";
    setStatus("llm-status", r.warning || ok, r.warning ? "warn" : "ok");
    loadProviders();
  } catch (e) { setStatus("llm-status", e.message, "err"); }
});

// ---- knowledge ----------------------------------------------------------------
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".kn-pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    el("pane-" + btn.dataset.kind).classList.add("active");
  });
});

function rowRemoveBtn(row) {
  const rm = document.createElement("button");
  rm.className = "small"; rm.textContent = "Remove";
  rm.addEventListener("click", () => row.remove());
  return rm;
}

function addFaqRow(q, a) {
  const row = document.createElement("div");
  row.className = "row";
  const qi = document.createElement("input"); qi.placeholder = "Question"; qi.value = q || "";
  const ai = document.createElement("input"); ai.placeholder = "Answer"; ai.value = a || "";
  row.appendChild(qi); row.appendChild(ai); row.appendChild(rowRemoveBtn(row));
  el("faq-rows").appendChild(row);
}
el("faq-add").addEventListener("click", () => addFaqRow("", ""));

function addPatternRow(p) {
  const row = document.createElement("div");
  row.className = "row3";
  const pi = document.createElement("input"); pi.placeholder = "pattern_name"; pi.value = (p && p.pattern) || "";
  const ei = document.createElement("input"); ei.placeholder = "examples, comma separated"; ei.value = (p && p.examples || []).join(", ");
  const ri = document.createElement("input"); ri.placeholder = "Reply"; ri.value = (p && p.reply) || "";
  row.appendChild(pi); row.appendChild(ei); row.appendChild(ri); row.appendChild(rowRemoveBtn(row));
  el("pat-rows").appendChild(row);
}
el("pat-add").addEventListener("click", () => addPatternRow(null));

const BIZ_FIELDS = ["store_name", "website", "instagram", "support_phone", "support_email", "support_hours"];
function buildBizFields() {
  const wrap = el("biz-fields");
  wrap.innerHTML = "";
  for (const f of BIZ_FIELDS) {
    const div = document.createElement("div");
    div.className = "field";
    const lab = document.createElement("label");
    lab.textContent = f.replace(/_/g, " ");
    const inp = document.createElement("input");
    inp.type = "text"; inp.id = "biz-" + f;
    div.appendChild(lab); div.appendChild(inp);
    wrap.appendChild(div);
  }
}

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

el("bv-save").addEventListener("click", async () => {
  try {
    await api("PUT", "/admin/knowledge/brand_voice", { content: el("bv-content").value });
    setStatus("bv-status", "Saved. Applies from the next customer message.", "ok");
  } catch (e) { setStatus("bv-status", e.message, "err"); }
});

el("faq-save").addEventListener("click", async () => {
  const items = [];
  el("faq-rows").querySelectorAll(".row").forEach((row) => {
    const [qi, ai] = row.querySelectorAll("input");
    if (qi.value.trim() && ai.value.trim()) items.push({ q: qi.value.trim(), a: ai.value.trim() });
  });
  try {
    await api("PUT", "/admin/knowledge/faq", { items });
    setStatus("faq-status", "Saved " + items.length + " questions.", "ok");
  } catch (e) { setStatus("faq-status", e.message, "err"); }
});

el("biz-save").addEventListener("click", async () => {
  const body = { note: el("biz-note").value.trim(), extra: {} };
  for (const f of BIZ_FIELDS) body[f] = el("biz-" + f).value.trim();
  try {
    await api("PUT", "/admin/knowledge/business", body);
    setStatus("biz-status", "Saved.", "ok");
  } catch (e) { setStatus("biz-status", e.message, "err"); }
});

el("pat-save").addEventListener("click", async () => {
  const items = [];
  el("pat-rows").querySelectorAll(".row3").forEach((row) => {
    const [pi, ei, ri] = row.querySelectorAll("input");
    const examples = ei.value.split(",").map((s) => s.trim()).filter(Boolean);
    if (pi.value.trim() && examples.length && ri.value.trim()) {
      items.push({ pattern: pi.value.trim(), examples, reply: ri.value.trim() });
    }
  });
  try {
    await api("PUT", "/admin/knowledge/patterns", { items });
    setStatus("pat-status", "Saved " + items.length + " patterns.", "ok");
  } catch (e) { setStatus("pat-status", e.message, "err"); }
});

// ---- controls -----------------------------------------------------------------
function csv(value) { return value.split(",").map((s) => s.trim()).filter(Boolean); }

async function loadControls() {
  const c = await api("GET", "/admin/controls");
  el("c-send-mode").value = c.send_mode;
  el("c-allowlist").value = c.allowlist_phones.join(", ");
  el("c-push-policy").value = c.push_policy;
  el("c-language").value = c.default_language;
  el("c-staleness").value = c.push_staleness_hours;
  el("c-retention").value = c.retention_days;
  el("c-vip-threshold").value = c.vip_order_count_threshold;
  el("c-base-url").value = c.public_base_url;
  el("c-owner-alert").value = c.owner_alert_number;
  el("c-rv-order").checked = c.reveal_fields.includes("order_number");
  el("c-rv-email").checked = c.reveal_fields.includes("email");
  el("c-rv-status").checked = c.reveal_fields.includes("status");
  el("c-rv-items").checked = c.reveal_fields.includes("items");
  el("c-rv-tracking").checked = c.reveal_fields.includes("tracking");
  el("c-tags-pending").value = c.tags.pending.join(", ");
  el("c-tags-confirmed").value = c.tags.confirmed.join(", ");
  el("c-tags-cancelled").value = c.tags.cancelled.join(", ");
}

el("c-save").addEventListener("click", async () => {
  const reveal = [];
  if (el("c-rv-order").checked) reveal.push("order_number");
  if (el("c-rv-email").checked) reveal.push("email");
  if (el("c-rv-status").checked) reveal.push("status");
  if (el("c-rv-items").checked) reveal.push("items");
  if (el("c-rv-tracking").checked) reveal.push("tracking");
  const body = {
    send_mode: el("c-send-mode").value,
    allowlist_phones: csv(el("c-allowlist").value),
    push_policy: el("c-push-policy").value,
    reveal_fields: reveal,
    tags: {
      pending: csv(el("c-tags-pending").value),
      confirmed: csv(el("c-tags-confirmed").value),
      cancelled: csv(el("c-tags-cancelled").value),
    },
    default_language: el("c-language").value,
    push_staleness_hours: parseInt(el("c-staleness").value, 10),
    retention_days: parseInt(el("c-retention").value, 10),
    vip_order_count_threshold: parseInt(el("c-vip-threshold").value, 10),
    public_base_url: el("c-base-url").value.trim(),
    owner_alert_number: el("c-owner-alert").value.trim(),
  };
  try {
    await api("PUT", "/admin/controls", body);
    setStatus("c-status", "Saved. Applies to the next webhook or send.", "ok");
  } catch (e) { setStatus("c-status", e.message, "err"); }
});

// ---- views -----------------------------------------------------------------
function fillTable(tableId, rows, cols) {
  const tbody = el(tableId).querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      td.textContent = r[c] === null || r[c] === undefined ? "" : String(r[c]);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

async function loadViews() {
  try {
    const maps = await api("GET", "/admin/mappings");
    fillTable("mappings-table", maps, ["order_name", "phone_e164", "status", "is_cod", "created_at"]);
    const outbox = await api("GET", "/admin/outbox");
    fillTable("outbox-table", outbox, ["dedupe_key", "state", "kind", "phone_e164", "attempts", "last_error_code"]);
    setStatus("views-status", "");
  } catch (e) { setStatus("views-status", e.message, "err"); }
}
el("views-refresh").addEventListener("click", loadViews);

// ---- boot -----------------------------------------------------------------
async function loadAll() {
  try {
    await Promise.all([
      loadShopify(), loadWhatsApp(), loadProviders(), loadKnowledge(), loadControls(),
      loadViews(),
    ]);
  } catch (e) { /* individual sections surface their own errors */ }
}

(async function boot() {
  try {
    await api("GET", "/admin/session");
    showPanel();
  } catch (e) { /* not logged in - stay on the login screen */ }
})();
