// Dedicated WhatsApp-style chat page - standalone, no shared state with admin.js.
"use strict";

function el(id) { return document.getElementById(id); }

async function api(path, method = "GET") {
  const opts = { method, credentials: "same-origin" };
  if (method !== "GET") {
    // A bodyless POST sends no Content-Length, which Vercel's edge rejects with a 411 before the
    // request reaches the app. Attach an empty JSON body so the edge lets it through; the FastAPI
    // route ignores the body.
    opts.headers = { "Content-Type": "application/json" };
    opts.body = "{}";
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    window.location.href = "/admin/ui/";
    throw new Error("not authenticated");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : res.status;
    throw new Error("Request failed: " + detail);
  }
  return data;
}

let currentThreadId = null;
let currentPhone = null;
let currentOrders = [];

const STATUS_LABELS = {
  suppressed: "Not delivered — skipped by send policy",
  failed: "Failed to send",
  undeliverable: "Undeliverable",
  queued: "Queued",
};

function renderBubble(entry) {
  const div = document.createElement("div");
  const side = entry.type === "customer_message" ? "bubble-in" : "bubble-out";
  div.className = "bubble " + side;
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent = entry.type.replace("_", " ");
  const text = document.createElement("div");
  text.textContent = entry.text;
  const ts = document.createElement("div");
  ts.className = "bubble-ts";
  ts.textContent = entry.timestamp || "";
  div.appendChild(label);
  div.appendChild(text);
  if (entry.status && entry.status !== "sent") {
    const status = document.createElement("div");
    status.className = "bubble-status";
    if (entry.status === "failed" || entry.status === "undeliverable") {
      status.classList.add("bubble-status-error");
    }
    status.textContent = STATUS_LABELS[entry.status] || entry.status;
    div.appendChild(status);
  }
  div.appendChild(ts);
  return div;
}

function renderOrderDetail(order) {
  el("order-number").textContent = order.order_name;
  const container = el("order-detail");
  container.innerHTML = "";
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
    link.textContent = "Track shipment";
    link.style.fontSize = ".8rem";
    container.appendChild(link);
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
}

function renderOrderPanel(orders) {
  currentOrders = orders;
  const empty = el("order-empty");
  const select = el("order-select");
  if (!orders.length) {
    empty.style.display = "block";
    select.style.display = "none";
    el("order-detail").innerHTML = "";
    // Also clear the Task 4 order-number + products, else switching from a thread WITH orders
    // to one WITHOUT leaves the previous customer's order data on screen (wrong-customer risk).
    el("order-number").textContent = "";
    el("order-products").innerHTML = "";
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
  currentThreadId = threadId;
  currentPhone = phone;
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
      for (const entry of data.entries) {
        container.appendChild(renderBubble(entry));
      }
      container.scrollTop = container.scrollHeight;
    }
    renderOrderPanel(data.orders);
    const resumeBtn = el("resume-ai-btn");
    const isPaused = data.paused_until && new Date(data.paused_until) > new Date();
    resumeBtn.style.display = isPaused ? "inline-block" : "none";
    threadSnapshotKey = threadEntriesKey(data.entries);
    if (!silent) el("thread-status").textContent = "";
  } catch (e) {
    // A poll-triggered load (silent) must never write thread-status, which is reserved for
    // explicit user-triggered load errors.
    if (!silent) el("thread-status").textContent = e.message;
  }
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
    renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
    listSnapshotKey = threadListKey(allThreads);
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}

el("thread-search").addEventListener("input", () => {
  renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
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

let listSnapshotKey = "";
let threadSnapshotKey = "";

function threadListKey(threads) {
  return threads.map((t) => t.thread_id + ":" + (t.last_active_at || "")).join("|");
}

function threadEntriesKey(entries) {
  if (!entries.length) return "empty";
  const last = entries[entries.length - 1];
  // Fold every entry's status into the key so a queued -> sent/failed/undeliverable transition
  // (which changes neither the count nor the last timestamp) is still detected by the poll diff.
  const statuses = entries.map((e) => e.status || "").join(",");
  return entries.length + ":" + (last.timestamp || "") + ":" + statuses;
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
      renderThreadRows(allThreads.filter((t) => threadMatchesQuery(t, el("thread-search").value)));
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

loadThreadList();
