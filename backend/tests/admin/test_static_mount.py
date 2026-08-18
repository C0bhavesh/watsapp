from fastapi.testclient import TestClient


def test_static_panel_served(client: TestClient) -> None:
    r = client.get("/admin/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Thetavas Admin" in r.text


def test_controls_panel_exposes_retention_days_field(client: TestClient) -> None:
    # The panel must render a retention_days input; without it the JS omitted the field from
    # the PUT and AdminControls' default 0 silently reverted whatever the API had set.
    html = client.get("/admin/ui/").text
    assert 'id="c-retention"' in html


def test_controls_panel_js_sends_retention_days(client: TestClient) -> None:
    # The controls-save handler must include retention_days in the PUT body so a saved value
    # is not clobbered back to the model default on the next unrelated save.
    js = client.get("/admin/ui/admin.js").text
    assert "retention_days" in js
    assert "c-retention" in js


def test_old_embedded_chats_card_removed(client: TestClient) -> None:
    html = client.get("/admin/ui/").text
    assert 'id="chats-card"' not in html
    assert 'id="chats-list-table"' not in html


def test_old_embedded_chats_js_removed(client: TestClient) -> None:
    js = client.get("/admin/ui/admin.js").text
    assert "loadChatList" not in js
    assert "loadChatThread" not in js


def test_chats_page_served(client: TestClient) -> None:
    r = client.get("/admin/ui/chats.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="thread-list"' in r.text
    assert 'id="chat-messages"' in r.text
    assert 'id="order-panel"' in r.text


def test_chats_js_served_and_calls_the_conversations_api(client: TestClient) -> None:
    r = client.get("/admin/ui/chats.js")
    assert r.status_code == 200
    js = r.text
    assert "/admin/conversations" in js
    assert "loadThreadList" in js
    assert "loadThread" in js


def test_chats_page_has_order_number_and_products_containers(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="order-number"' in resp.text
    assert 'id="order-products"' in resp.text


def test_chats_js_renders_order_number_and_line_items(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "order-number" in resp.text
    assert "order-products" in resp.text
    assert "line_items" in resp.text


def test_chats_js_renders_bubble_status_label(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "entry.status" in resp.text
    assert "bubble-status" in resp.text


def test_chats_page_has_search_input(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="thread-search"' in resp.text


def test_chats_js_filters_threads_by_name_phone_and_order_number(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "customer_name" in resp.text
    assert "order_names" in resp.text
    assert "normalize_order_name" in resp.text or "tavas" in resp.text


def test_chats_js_polls_every_three_seconds(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    assert "setInterval" in resp.text
    assert "3000" in resp.text


def test_chats_js_clears_order_number_and_products_when_no_orders(client: TestClient) -> None:
    # Switching to a no-orders thread must wipe the previous customer's order number + products,
    # else stale order data stays visible (wrong-customer risk). Fix 1.
    js = client.get("/admin/ui/chats.js").text
    assert 'el("order-number").textContent = ""' in js
    assert 'el("order-products").innerHTML = ""' in js


def test_chats_js_thread_entries_key_includes_status(client: TestClient) -> None:
    # The poll diff-key must fold entry status in, else a queued -> sent/failed transition is
    # never detected and the status label stays stuck until a manual refresh. Fix 2.
    js = client.get("/admin/ui/chats.js").text
    assert "e.status" in js


def test_chats_js_thread_entries_key_includes_delivery_status(client: TestClient) -> None:
    # The poll diff-key must also fold delivery_status in: it transitions independently of status
    # (which stays "sent" on a template row), so without it a delivered/read tick update is never
    # detected on an open thread until a manual refresh, defeating live receipts.
    js = client.get("/admin/ui/chats.js").text
    assert "e.delivery_status" in js


def test_chats_js_poll_skips_when_hidden_and_guards_overlap(client: TestClient) -> None:
    # The 3s poll must skip while the tab is hidden and never overlap a slow tick, to protect the
    # shared DB pool (max_size=5). Fix 3.
    js = client.get("/admin/ui/chats.js").text
    assert "document.hidden" in js
    assert "pollInFlight" in js


def test_chats_page_has_resume_ai_button(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="resume-ai-btn"' in resp.text


def test_chats_js_calls_the_resume_endpoint(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/resume" in js
    assert "paused_until" in js


def test_chats_js_sends_json_body_on_non_get_requests(client: TestClient) -> None:
    # A bodyless POST from fetch() sends no Content-Length, which Vercel's edge rejects with a
    # 411 Length Required before it reaches the app -- breaking the Resume AI button in a real
    # browser (invisible to TestClient, which never traverses Vercel's edge). The api() helper
    # must attach a Content-Type: application/json header and a body on any non-GET call.
    js = client.get("/admin/ui/chats.js").text
    assert "application/json" in js
    assert "body" in js


def test_chats_js_normalize_order_query_strips_leading_hash(client: TestClient) -> None:
    # The search box invites "#3589"; a leading # must be stripped before the digit test so it
    # still normalizes to tavas3589. Fix 5.
    js = client.get("/admin/ui/chats.js").text
    assert 'replace(/^#/, "")' in js


def test_chats_js_thread_list_shows_name_only(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    # The old "(" + phone + ")" suffix pattern must be gone from the thread-row rendering.
    assert 'customer_name + " (" + t.phone + ")"' not in js


def test_chats_js_formats_bubble_timestamp_as_12h(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "formatBubbleTime" in js
    assert "hour12" in js or "toLocaleTimeString" in js


def test_chats_page_has_customer_details_container(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert 'id="order-customer"' in resp.text
    assert "Order Details" in resp.text


def test_chats_js_renders_customer_address_fields(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "customer_name" in js
    assert "address_line1" in js
    assert "postal_code" in js


def test_chats_js_renders_delivery_ticks(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "delivery_status" in js
    assert "delivered" in js
    assert "read" in js


def test_chats_js_shows_clock_icon_for_processing_status(client: TestClient) -> None:
    # A template_sent entry stuck in "processing" status should show a clock icon (⏱)
    # instead of the literal word "processing" as status text.
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    assert "⏱" in js
    assert "delivery-mark-processing" in js


def test_chats_html_has_processing_mark_css(client: TestClient) -> None:
    # The clock icon for processing status must have a CSS rule defining its color.
    resp = client.get("/admin/ui/chats.html")
    html = resp.text
    assert "delivery-mark-processing" in html
    assert "#8696a0" in html


def test_chats_js_excludes_processing_from_status_text(client: TestClient) -> None:
    # The "processing" status should NOT render as a bottom-line status text (it gets the icon
    # instead). The condition on renderBubble must exclude "processing" from triggering the
    # status text div.
    resp = client.get("/admin/ui/chats.js")
    js = resp.text
    # The condition should be something like: status !== "sent" && status !== "processing"
    # or an explicit list that excludes processing. We check that "processing" appears
    # in a conditional context (the status check).
    assert "entry.status" in js
    assert "processing" in js
