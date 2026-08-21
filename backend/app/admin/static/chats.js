// Dedicated WhatsApp-style chat page - standalone, no shared state with admin.js.
"use strict";

function el(id) { return document.getElementById(id); }

async function api(path, method = "GET", body = null) {
  const opts = { method, credentials: "same-origin" };
  if (method !== "GET") {
    // A bodyless POST sends no Content-Length, which Vercel's edge rejects with a 411 before the
    // request reaches the app. Default to an empty JSON body so the edge lets it through even when
    // the caller has nothing to send; a caller with real data passes it via `body`.
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body === null ? {} : body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    window.location.href = "/admin/ui/";
    throw new Error("not authenticated");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    // Most endpoints return {"detail": ...} (a string or, for validation errors, an object -> keep
    // JSON.stringify); the manual-reply send endpoint instead returns {"ok": false, "error": "..."}
    // (always a plain string) on a 502/503, so prefer data.error as a plain-string fallback.
    let detail = res.status;
    if (data && data.detail) {
      detail = JSON.stringify(data.detail);
    } else if (data && data.error) {
      detail = data.error;
    }
    throw new Error("Request failed: " + detail);
  }
  return data;
}

let currentThreadId = null;
let currentPhone = null;
let currentOrders = [];
// Tracks whether the currently-loaded thread is still inside the 24h reply window. Set by
// loadThread; read by the send handler's finally so it re-enables Send only when a reply is allowed.
let replyWithinWindow = false;

const STATUS_LABELS = {
  suppressed: "Not delivered — skipped by send policy",
  failed: "Failed to send",
  undeliverable: "Undeliverable",
  queued: "Queued",
};

// A template resend's success outcome (Finding 4): "sent" closes the dialog; any other outcome is
// NOT delivered, so we keep the dialog open and say so rather than closing as if it went out.
const TEMPLATE_STATUS_NOTES = {
  queued: "Queued — send mode is off. It will go out when sending is enabled.",
  suppressed: "Withheld by send policy (shadow/allowlist mode) — not delivered.",
};

function formatBubbleTime(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true })
    .toLowerCase();
}

function formatBubbleDate(isoTimestamp) {
  if (!isoTimestamp) return "";
  const d = new Date(isoTimestamp);
  if (Number.isNaN(d.getTime())) return "";
  const day = String(d.getDate()).padStart(2, "0");
  const month = String(d.getMonth() + 1).padStart(2, "0");
  return day + "/" + month + "/" + d.getFullYear();
}

const REPLY_WINDOW_MS = 24 * 60 * 60 * 1000;

const EMOJI_LIST = [
  "😀", "😂", "😊", "😍", "🙏", "👍", "👎", "🙌",
  "🎉", "❤️", "🔥", "✅", "❌", "⏳", "📦", "🚚",
  "😢", "😡", "🤔", "😅", "🙂", "😎", "💯", "🤝",
  "📞", "📍", "💳", "🛍️", "⭐", "🙁", "👋", "🎁",
];

function buildEmojiPopup() {
  const popup = el("emoji-popup");
  popup.innerHTML = "";
  for (const emoji of EMOJI_LIST) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = emoji;
    btn.addEventListener("click", () => insertAtCursor(el("reply-input"), emoji));
    popup.appendChild(btn);
  }
}

function insertAtCursor(input, text) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  const newPos = start + text.length;
  input.focus();
  input.setSelectionRange(newPos, newPos);
}

buildEmojiPopup();

el("emoji-btn").addEventListener("click", () => {
  const popup = el("emoji-popup");
  popup.style.display = popup.style.display === "none" ? "grid" : "none";
});

document.addEventListener("click", (e) => {
  const popup = el("emoji-popup");
  if (popup.style.display === "none") return;
  if (e.target === el("emoji-btn") || popup.contains(e.target)) return;
  popup.style.display = "none";
});

function closeTemplateDialog() {
  el("template-dialog").style.display = "none";
}

function renderTemplateFieldForm(orderName, tmpl) {
  const body = el("template-dialog-body");
  body.innerHTML = "";
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "← Back";
  back.addEventListener("click", () => openTemplateDialog());
  body.appendChild(back);

  const heading = document.createElement("div");
  heading.className = "template-order-heading";
  heading.textContent = tmpl.label + " — " + orderName;
  body.appendChild(heading);

  const inputs = {};
  for (const field of tmpl.fields) {
    const row = document.createElement("div");
    row.className = "template-field-row";
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || "";
    // Finding 3: the server pins order_id to the resolved order name regardless of what is
    // submitted, so show it read-only rather than as an editable field that is silently overridden.
    if (field.read_only) input.disabled = true;
    inputs[field.key] = input;
    row.appendChild(label);
    row.appendChild(input);
    body.appendChild(row);
  }

  const sendBtn = document.createElement("button");
  sendBtn.id = "template-send-btn";
  sendBtn.type = "button";
  sendBtn.textContent = "Send";
  // Finding 2: the template dialog is a full-viewport overlay, so the error must land INSIDE it
  // (a previous version wrote to #reply-status, which sits BEHIND the modal and was invisible).
  const templateStatus = document.createElement("div");
  templateStatus.id = "template-status";
  templateStatus.className = "template-status";
  sendBtn.addEventListener("click", async () => {
    if (currentThreadId === null) return;
    sendBtn.disabled = true;
    templateStatus.textContent = "";
    const values = {};
    for (const key in inputs) values[key] = inputs[key].value;
    try {
      const result = await api(
        "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/templates",
        "POST",
        { order_name: orderName, template: tmpl.key, values }
      );
      await loadThread(currentThreadId, currentPhone);
      const sendStatus = result && result.status;
      if (sendStatus && sendStatus !== "sent") {
        // Enqueued or withheld, not delivered: keep the dialog open, say so, and leave Send
        // disabled -- the row is already queued, a second click would double-send (Finding 4).
        templateStatus.textContent =
          TEMPLATE_STATUS_NOTES[sendStatus] || ("Not sent: " + sendStatus);
      } else {
        closeTemplateDialog();
      }
    } catch (e) {
      // Finding 2: show the failure in the dialog, and relabel to "Retry" so an accidental
      // second click cannot silently fire a duplicate send -- retrying is now a deliberately
      // different action, and a rapid double-click is already blocked by the disable-on-start.
      templateStatus.textContent = e.message;
      sendBtn.textContent = "Retry";
      sendBtn.disabled = false;
    }
  });
  body.appendChild(sendBtn);
  body.appendChild(templateStatus);
}

function renderTemplatePickList(data) {
  const body = el("template-dialog-body");
  body.innerHTML = "";
  if (!data.orders.length) {
    body.textContent = "No orders found for this customer.";
    return;
  }

  // One <option> per order; data.orders is already most-recent-first from the API,
  // so option 0 (pre-selected) is the newest order -- no extra sorting needed.
  const select = document.createElement("select");
  select.className = "template-order-select";
  data.orders.forEach((orderEntry, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = orderEntry.order_name;
    select.appendChild(option);
  });
  body.appendChild(select);

  const list = document.createElement("div");
  list.className = "template-pick-list";
  body.appendChild(list);

  // Re-render only the list container for the selected order; all orders' templates
  // are already in `data`, so switching orders needs no extra network call.
  function renderTemplatesFor(index) {
    const orderEntry = data.orders[index];
    list.innerHTML = "";
    if (!orderEntry.templates.length) {
      list.textContent = "No templates available for this order.";
      return;
    }
    for (const tmpl of orderEntry.templates) {
      const row = document.createElement("div");
      row.className = "template-pick-row";
      row.textContent = tmpl.label;
      row.addEventListener("click", () => renderTemplateFieldForm(orderEntry.order_name, tmpl));
      list.appendChild(row);
    }
  }

  select.addEventListener("change", () => renderTemplatesFor(Number(select.value)));
  renderTemplatesFor(0);
}

async function openTemplateDialog() {
  if (currentThreadId === null) return;
  el("template-dialog").style.display = "flex";
  el("template-dialog-body").textContent = "Loading…";
  try {
    const data = await api(
      "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/templates"
    );
    renderTemplatePickList(data);
  } catch (e) {
    el("template-dialog-body").textContent = e.message;
  }
}

el("template-btn").addEventListener("click", openTemplateDialog);
el("template-dialog-close").addEventListener("click", closeTemplateDialog);

function lastCustomerMessageAt(entries) {
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i].type === "customer_message") return new Date(entries[i].timestamp);
  }
  return null;
}

function renderDeliveryMark(entry) {
  // A tick makes sense for a template_sent entry that actually went out (status "sent"), for
  // any ai_reply entry (which carries no status field to gate on), or for a template_sent stuck
  // in "processing" (which shows a clock icon instead of the tick).
  const eligible =
    entry.type === "ai_reply" ||
    (entry.type === "template_sent" && (entry.status === "sent" || entry.status === "processing"));
  if (!eligible) return null;
  const mark = document.createElement("span");
  mark.className = "delivery-mark";
  if (entry.status === "processing") {
    mark.textContent = "⏱";
    mark.classList.add("delivery-mark-processing");
  } else if (entry.delivery_status === "failed") {
    mark.textContent = "!";
    mark.classList.add("delivery-mark-failed");
  } else if (entry.delivery_status === "read") {
    mark.textContent = "✓✓";
    mark.classList.add("delivery-mark-read");
  } else if (entry.delivery_status === "delivered") {
    mark.textContent = "✓✓";
    mark.classList.add("delivery-mark-delivered");
  } else {
    mark.textContent = "✓";
    mark.classList.add("delivery-mark-sent");
  }
  return mark;
}

function renderBubble(entry) {
  const div = document.createElement("div");
  const side = entry.type === "customer_message" ? "bubble-in" : "bubble-out";
  div.className = "bubble " + side;
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent =
    entry.type === "ai_reply" && entry.sender === "admin" ? "you" : entry.type.replace("_", " ");
  const text = document.createElement("div");
  text.textContent = entry.text;
  const ts = document.createElement("div");
  ts.className = "bubble-ts";
  ts.textContent = formatBubbleTime(entry.timestamp);
  const mark = renderDeliveryMark(entry);
  if (mark) ts.appendChild(mark);
  div.appendChild(label);
  div.appendChild(text);
  if (entry.status && entry.status !== "sent" && entry.status !== "processing") {
    const status = document.createElement("div");
    status.className = "bubble-status";
    if (
      entry.status === "failed" ||
      entry.status === "undeliverable" ||
      entry.status === "suppressed"
    ) {
      status.classList.add("bubble-status-error");
    }
    status.textContent = STATUS_LABELS[entry.status] || entry.status;
    div.appendChild(status);
  }
  div.appendChild(ts);
  return div;
}

function renderDateDivider(dateLabel) {
  const div = document.createElement("div");
  div.className = "date-divider";
  div.textContent = dateLabel;
  return div;
}

function renderOrderDetail(order) {
  el("order-number").textContent = order.order_name;

  const customerContainer = el("order-customer");
  customerContainer.innerHTML = "";
  if (order.customer_name || order.address_line1 || order.city) {
    const heading = document.createElement("h4");
    heading.textContent = "Customer Details";
    customerContainer.appendChild(heading);
    const custFields = [
      ["Name", order.customer_name],
      ["Address", [order.address_line1, order.address_line2].filter(Boolean).join(", ")],
      ["City", order.city],
      ["State", order.state],
      ["Pincode", order.postal_code],
    ];
    for (const [label, value] of custFields) {
      if (!value) continue;
      const row = document.createElement("div");
      row.className = "order-field";
      row.innerHTML = "<span class='label'>" + label + ":</span> ";
      row.appendChild(document.createTextNode(value));
      customerContainer.appendChild(row);
    }
  }

  const productsContainer = el("order-products");
  productsContainer.innerHTML = "";
  const items = order.line_items || [];
  if (items.length) {
    const heading = document.createElement("h4");
    heading.textContent = "Products";
    productsContainer.appendChild(heading);
    for (const li of items) {
      const row = document.createElement("div");
      row.className = "product-row";
      let text = li.quantity + "× " + li.title;
      if (li.variant_title) text += " (" + li.variant_title + ")";
      if (li.price_amount) text += " — " + li.price_amount + " " + (li.price_currency || "");
      row.textContent = text;
      productsContainer.appendChild(row);
    }
  }

  const container = el("order-detail");
  container.innerHTML = "";
  const fulfillHeading = document.createElement("h4");
  fulfillHeading.textContent = "Fulfillment Details";
  container.appendChild(fulfillHeading);
  const fields = [
    ["Status", order.financial_status || "-"],
    ["Fulfillment", order.fulfillment_status || "not dispatched"],
    ["Cancelled", order.cancelled_at ? "yes" : "no"],
    ["Payment", order.is_cod ? "COD" : "prepaid"],
    ["Amount", order.total_amount ? order.total_amount + " " + (order.total_currency || "") : "-"],
    ["Tags", order.tags && order.tags.length ? order.tags.join(", ") : "-"],
    ["Courier", order.tracking_company || "-"],
    ["Tracking #", order.tracking_number || "-"],
  ];
  for (const [label, value] of fields) {
    const row = document.createElement("div");
    row.className = "order-field";
    row.innerHTML = "<span class='label'>" + label + ":</span> ";
    row.appendChild(document.createTextNode(value));
    container.appendChild(row);
  }
  if (order.tracking_url && /^https?:\/\//i.test(order.tracking_url)) {
    const link = document.createElement("a");
    link.href = order.tracking_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = order.tracking_url;
    link.style.fontSize = ".75rem";
    link.style.wordBreak = "break-all";
    container.appendChild(link);
  }

  renderExchangeDetail(order);
}

// The 6 fixed stages of ExchangeStatus (app/core/exchange_models.py) -- no courier/QC
// integration exists, so this is the only place either field advances (via POST /admin/exchanges/{id}).
const EXCHANGE_STATUSES = [
  "requested", "return_picked_up", "qc_passed", "qc_failed",
  "replacement_dispatched", "delivered",
];

function renderExchangeDetail(order) {
  const container = el("order-exchange");
  container.innerHTML = "";
  if (!order.exchange) {
    container.style.display = "none";
    return;
  }
  container.style.display = "block";
  const heading = document.createElement("h4");
  heading.textContent = "Exchange";
  container.appendChild(heading);

  const infoFields = [
    ["Requested size", order.exchange.requested_size],
    // requested_at is a raw ISO-8601 timestamp -- formatBubbleDate is this file's existing
    // convention for rendering an ISO timestamp as a readable date (used for the chat date dividers).
    ["Requested on", formatBubbleDate(order.exchange.requested_at)],
  ];
  for (const [label, value] of infoFields) {
    if (!value) continue;
    const row = document.createElement("div");
    row.className = "order-field";
    row.innerHTML = "<span class='label'>" + label + ":</span> ";
    row.appendChild(document.createTextNode(value));
    container.appendChild(row);
  }

  const statusSelect = document.createElement("select");
  statusSelect.className = "exchange-status-select";
  for (const s of EXCHANGE_STATUSES) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s.replace(/_/g, " ");
    if (s === order.exchange.status) opt.selected = true;
    statusSelect.appendChild(opt);
  }
  container.appendChild(statusSelect);

  const trackingLabel = document.createElement("div");
  trackingLabel.className = "order-field";
  trackingLabel.innerHTML = "<span class='label'>Return tracking URL:</span>";
  container.appendChild(trackingLabel);

  const trackingInput = document.createElement("input");
  trackingInput.type = "text";
  trackingInput.className = "exchange-tracking-input";
  trackingInput.placeholder = "Return tracking URL";
  trackingInput.title = "Return tracking URL";
  trackingInput.setAttribute("aria-label", "Return tracking URL");
  trackingInput.value = order.exchange.return_tracking_url || "";
  container.appendChild(trackingInput);

  const replacementTrackingLabel = document.createElement("div");
  replacementTrackingLabel.className = "order-field";
  replacementTrackingLabel.innerHTML = "<span class='label'>Replacement order tracking URL:</span>";
  container.appendChild(replacementTrackingLabel);

  const replacementTrackingInput = document.createElement("input");
  replacementTrackingInput.type = "text";
  replacementTrackingInput.className = "exchange-tracking-input";
  replacementTrackingInput.placeholder = "Replacement order tracking URL";
  replacementTrackingInput.title = "Replacement order tracking URL";
  replacementTrackingInput.setAttribute("aria-label", "Replacement order tracking URL");
  replacementTrackingInput.value = order.exchange.replacement_tracking_url || "";
  container.appendChild(replacementTrackingInput);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "exchange-save-btn";
  saveBtn.textContent = "Save";
  const saveStatus = document.createElement("div");
  saveStatus.className = "exchange-status-msg";
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "";
    try {
      await api("/admin/exchanges/" + encodeURIComponent(order.exchange.id), "POST", {
        status: statusSelect.value,
        return_tracking_url: trackingInput.value || null,
        replacement_tracking_url: replacementTrackingInput.value || null,
      });
    } catch (e) {
      // Mirrors reply-status/template-status: empty on success, error text on failure.
      saveStatus.textContent = e.message;
    } finally {
      saveBtn.disabled = false;
    }
  });
  container.appendChild(saveBtn);
  container.appendChild(saveStatus);
}

function renderOrderPanel(orders) {
  currentOrders = orders;
  const empty = el("order-empty");
  const select = el("order-select");
  if (!orders.length) {
    empty.style.display = "block";
    select.style.display = "none";
    el("order-detail").innerHTML = "";
    // Also clear the order-number + customer + products, else switching from a thread WITH orders
    // to one WITHOUT leaves the previous customer's order data on screen (wrong-customer risk).
    el("order-number").textContent = "";
    el("order-customer").innerHTML = "";
    el("order-products").innerHTML = "";
    el("order-exchange").innerHTML = "";
    el("order-exchange").style.display = "none";
    return;
  }
  empty.style.display = "none";
  select.style.display = orders.length > 1 ? "block" : "none";
  select.innerHTML = "";
  orders.forEach((o, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = o.order_name;
    select.appendChild(opt);
  });
  select.onchange = () => renderOrderDetail(currentOrders[Number(select.value)]);
  renderOrderDetail(orders[0]);
}

async function loadThread(threadId, phone, silent = false) {
  // Capture the OLD thread id BEFORE reassigning, so a genuine thread switch (not the 3s silent
  // poll refresh of the SAME thread) clears any in-progress draft + stale status. Without this a
  // draft typed for customer A survives into customer B's thread and a single Enter could misfire.
  const isThreadSwitch = threadId !== currentThreadId;
  currentThreadId = threadId;
  currentPhone = phone;
  if (isThreadSwitch) {
    el("reply-input").value = "";
    el("reply-status").textContent = "";
  }
  document.querySelectorAll(".thread-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.threadId === String(threadId));
  });
  const threadMeta = allThreads.find((t) => t.thread_id === threadId);
  const headerName = threadMeta && threadMeta.customer_name;
  el("chat-header-phone").textContent =
    headerName ? headerName + " (" + (phone || "") + ")" : (phone || "");
  try {
    const data = await api("/admin/conversations/" + encodeURIComponent(threadId));
    const container = el("chat-messages");
    container.innerHTML = "";
    if (!data.entries.length) {
      container.innerHTML = '<div id="chat-empty">No messages yet</div>';
    } else {
      let lastRenderedDate = "";
      for (const entry of data.entries) {
        const entryDate = formatBubbleDate(entry.timestamp);
        if (entryDate && entryDate !== lastRenderedDate) {
          container.appendChild(renderDateDivider(entryDate));
          lastRenderedDate = entryDate;
        }
        container.appendChild(renderBubble(entry));
      }
      container.scrollTop = container.scrollHeight;
    }
    renderOrderPanel(data.orders);
    const resumeBtn = el("resume-ai-btn");
    const isPaused = data.paused_until && new Date(data.paused_until) > new Date();
    resumeBtn.style.display = isPaused ? "inline-block" : "none";
    const lastCustomerAt = lastCustomerMessageAt(data.entries);
    const withinWindow = lastCustomerAt && (Date.now() - lastCustomerAt.getTime()) < REPLY_WINDOW_MS;
    // Remember the window state so the send handler's finally re-enables the button only when a
    // reply is still allowed, instead of unconditionally (which left an enabled Send button on a
    // thread that had since scrolled outside the 24h window).
    replyWithinWindow = Boolean(withinWindow);
    const replyInput = el("reply-input");
    const replySendBtn = el("reply-send-btn");
    replyInput.disabled = !withinWindow;
    replySendBtn.disabled = !withinWindow;
    replyInput.placeholder = withinWindow
      ? "Type a message"
      : "Outside the 24-hour reply window — send a template instead";
    threadSnapshotKey = threadEntriesKey(data.entries);
    if (!silent) el("thread-status").textContent = "";
  } catch (e) {
    // A poll-triggered load (silent) must never write thread-status, which is reserved for
    // explicit user-triggered load errors.
    if (!silent) el("thread-status").textContent = e.message;
  }
}

const FILTERS = [
  { id: "all", label: "All", predicate: () => true },
  { id: "unread", label: "Unread", predicate: (t) => (t.unread_count || 0) > 0 },
  { id: "handoff", label: "Handed to human", predicate: (t) => !!t.ai_paused },
];
let activeFilterId = "all";

function renderFilterChips() {
  const container = el("thread-filters");
  container.innerHTML = "";
  for (const f of FILTERS) {
    const btn = document.createElement("button");
    btn.className = "filter-chip" + (f.id === activeFilterId ? " active" : "");
    btn.textContent = f.label;
    btn.addEventListener("click", () => {
      activeFilterId = f.id;
      renderFilterChips();
      renderThreadRows(applyThreadFilters(allThreads));
    });
    container.appendChild(btn);
  }
}

function applyThreadFilters(threads) {
  const chip = FILTERS.find((f) => f.id === activeFilterId) || FILTERS[0];
  const query = el("thread-search").value;
  return threads.filter((t) => chip.predicate(t) && threadMatchesQuery(t, query));
}

let allThreads = [];

function normalizeOrderQuery(query) {
  // Mirrors app/shopify/models.py::normalize_order_name's isdigit() branch: bare digits like
  // "3589" (or "#3589", matching the search box placeholder) should match the store's
  // "tavas3589" order-name format.
  const stripped = query.replace(/^#/, "");
  return /^\d+$/.test(stripped) ? "tavas" + stripped : query;
}

function threadMatchesQuery(thread, query) {
  if (!query) return true;
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if ((thread.phone || "").toLowerCase().includes(q)) return true;
  if ((thread.customer_name || "").toLowerCase().includes(q)) return true;
  const orderNames = (thread.order_names || []).map((n) => n.toLowerCase());
  if (orderNames.some((n) => n.includes(q))) return true;
  const normalized = normalizeOrderQuery(q);
  return orderNames.some((n) => n.includes(normalized));
}

function renderThreadRows(threads) {
  const list = el("thread-list");
  list.innerHTML = "";
  for (const t of threads) {
    const row = document.createElement("div");
    row.className = "thread-row";
    row.dataset.threadId = String(t.thread_id);
    if (t.thread_id === currentThreadId) row.classList.add("active");
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = t.last_active_at ? t.last_active_at.slice(0, 10) : "";
    const phone = document.createElement("div");
    phone.className = "phone";
    phone.textContent = t.customer_name || t.phone;
    if (t.unread_count > 0) {
      const badge = document.createElement("span");
      badge.className = "unread-badge";
      badge.textContent = String(t.unread_count);
      phone.appendChild(badge);
    }
    phone.appendChild(ts);
    const preview = document.createElement("div");
    preview.className = "preview";
    preview.textContent = t.preview || "";
    row.appendChild(phone);
    row.appendChild(preview);
    row.addEventListener("click", () => loadThread(t.thread_id, t.phone));
    list.appendChild(row);
  }
}

async function loadThreadList() {
  try {
    allThreads = await api("/admin/conversations");
    renderThreadRows(applyThreadFilters(allThreads));
    listSnapshotKey = threadListKey(allThreads);
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}

el("thread-search").addEventListener("input", () => {
  renderThreadRows(applyThreadFilters(allThreads));
});

el("refresh-btn").addEventListener("click", async () => {
  await loadThreadList();
  if (currentThreadId !== null) {
    await loadThread(currentThreadId, currentPhone);
  }
});

el("resume-ai-btn").addEventListener("click", async () => {
  if (currentThreadId === null) return;
  await api("/admin/conversations/" + encodeURIComponent(currentThreadId) + "/resume", "POST");
  await loadThread(currentThreadId, currentPhone);
});

el("reply-send-btn").addEventListener("click", async () => {
  if (currentThreadId === null) return;
  const input = el("reply-input");
  const text = input.value.trim();
  if (!text) return;
  const btn = el("reply-send-btn");
  btn.disabled = true;
  el("reply-status").textContent = "";
  try {
    await api(
      "/admin/conversations/" + encodeURIComponent(currentThreadId) + "/messages",
      "POST",
      { text }
    );
    input.value = "";
    await loadThread(currentThreadId, currentPhone);
  } catch (e) {
    el("reply-status").textContent = e.message;
  } finally {
    // Re-enable Send only if the thread is still within the 24h reply window; a thread that scrolled
    // out of the window (loadThread would have disabled the input) must not keep an enabled button.
    btn.disabled = !replyWithinWindow;
  }
});

el("reply-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") el("reply-send-btn").click();
});

let listSnapshotKey = "";
let threadSnapshotKey = "";

function threadListKey(threads) {
  return threads
    .map((t) => t.thread_id + ":" + (t.last_active_at || "") + ":" + t.unread_count + ":" + t.ai_paused)
    .join("|");
}

function threadEntriesKey(entries) {
  if (!entries.length) return "empty";
  const last = entries[entries.length - 1];
  // Fold every entry's status into the key so a queued -> sent/failed/undeliverable transition
  // (which changes neither the count nor the last timestamp) is still detected by the poll diff.
  const statuses = entries.map((e) => e.status || "").join(",");
  // Fold delivery_status in too: it transitions null -> delivered -> read independently of
  // status (which stays "sent" for a template row), so without this the poll never detects a
  // delivery/read change and the tick marks go stale on an open thread until a manual refresh.
  const deliveryStatuses = entries.map((e) => e.delivery_status || "").join(",");
  return entries.length + ":" + (last.timestamp || "") + ":" + statuses + ":" + deliveryStatuses;
}

let pollInFlight = false;

async function pollTick() {
  // Skip while the tab is hidden -- no point hammering the shared DB pool (max_size=5) with
  // 3s ticks for a backgrounded admin tab that nobody is watching.
  if (document.hidden) return;
  // In-flight guard: a slow tick must not overlap with the next scheduled one and double the load.
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const threads = await api("/admin/conversations");
    const nextListKey = threadListKey(threads);
    if (nextListKey !== listSnapshotKey) {
      allThreads = threads;
      listSnapshotKey = nextListKey;
      renderThreadRows(applyThreadFilters(allThreads));
    }
    if (currentThreadId !== null) {
      const data = await api("/admin/conversations/" + encodeURIComponent(currentThreadId));
      const nextThreadKey = threadEntriesKey(data.entries);
      if (nextThreadKey !== threadSnapshotKey) {
        threadSnapshotKey = nextThreadKey;
        await loadThread(currentThreadId, currentPhone, true);
      }
    }
  } catch (e) {
    // Silent -- a transient poll failure shouldn't overwrite list-status/thread-status, which are
    // reserved for explicit user-triggered load errors.
  } finally {
    pollInFlight = false;
  }
}

setInterval(pollTick, 3000);

renderFilterChips();
loadThreadList();
