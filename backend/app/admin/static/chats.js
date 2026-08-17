// Dedicated WhatsApp-style chat page - standalone, no shared state with admin.js.
"use strict";

function el(id) { return document.getElementById(id); }

async function api(path) {
  const res = await fetch(path, { method: "GET", credentials: "same-origin" });
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

async function loadThread(threadId, phone) {
  currentThreadId = threadId;
  currentPhone = phone;
  document.querySelectorAll(".thread-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.threadId === String(threadId));
  });
  el("chat-header").textContent = phone || "";
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
    el("thread-status").textContent = "";
  } catch (e) {
    el("thread-status").textContent = e.message;
  }
}

async function loadThreadList() {
  try {
    const threads = await api("/admin/conversations");
    const list = el("thread-list");
    list.innerHTML = "";
    for (const t of threads) {
      const row = document.createElement("div");
      row.className = "thread-row";
      row.dataset.threadId = String(t.thread_id);
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = t.last_active_at ? t.last_active_at.slice(0, 10) : "";
      const phone = document.createElement("div");
      phone.className = "phone";
      phone.textContent = t.phone;
      phone.appendChild(ts);
      const preview = document.createElement("div");
      preview.className = "preview";
      preview.textContent = t.preview || "";
      row.appendChild(phone);
      row.appendChild(preview);
      row.addEventListener("click", () => loadThread(t.thread_id, t.phone));
      list.appendChild(row);
    }
    el("list-status").textContent = "";
  } catch (e) {
    el("list-status").textContent = e.message;
  }
}

el("refresh-btn").addEventListener("click", async () => {
  await loadThreadList();
  if (currentThreadId !== null) {
    await loadThread(currentThreadId, currentPhone);
  }
});

loadThreadList();
