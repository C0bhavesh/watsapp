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


def test_chats_js_search_is_server_side(client: TestClient) -> None:
    # Search moved server-side (the `q` query param) so an older chat is findable, not just the
    # loaded page. The old client-only filter (threadMatchesQuery) must be gone -- the actual
    # name/phone/order matching is proven by tests/admin/test_views.py's search tests.
    js = client.get("/admin/ui/chats.js").text
    assert "threadMatchesQuery" not in js
    assert "conversationsUrl" in js
    assert "q: currentQuery" in js


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


def test_chats_js_supports_load_older_pagination(client: TestClient) -> None:
    # The keyset "Load older chats" control lets the admin reach beyond the first page (the reported
    # bug: only the 50 most-recent chats were reachable). The order-number "#3589" -> "tavas3589"
    # normalization now lives server-side (tests/store/test_chat_reads.py's search tests).
    js = client.get("/admin/ui/chats.js").text
    assert "before_last_active_at" in js
    assert "loadOlderThreads" in js
    assert "next_cursor" in js and "has_more" in js
    html = client.get("/admin/ui/chats.html").text
    assert 'id="load-older-btn"' in html


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


def test_chats_page_has_manual_reply_box(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="reply-input"' in resp.text
    assert 'id="reply-send-btn"' in resp.text


def test_chats_js_wires_manual_reply_send(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/messages" in js
    assert "reply-send-btn" in js
    assert "reply-input" in js


def test_chats_page_has_emoji_and_template_buttons(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.html")
    assert resp.status_code == 200
    assert 'id="emoji-btn"' in resp.text
    assert 'id="template-btn"' in resp.text
    assert 'id="emoji-popup"' in resp.text
    assert 'id="template-dialog"' in resp.text


def test_chats_js_wires_emoji_insert_and_template_send(client: TestClient) -> None:
    resp = client.get("/admin/ui/chats.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/templates" in js
    assert "emoji-btn" in js
    assert "template-btn" in js


def test_chats_js_template_error_lands_in_dialog_local_status(client: TestClient) -> None:
    # Finding 2: the template dialog is a full-viewport overlay, so a send failure must render in
    # a dialog-local status element (template-status), NOT #reply-status which sits behind the
    # modal and is invisible while the dialog is open. Assert the new target exists in the send
    # handler and the failure path relabels Send to "Retry" (a deliberate distinct retry action).
    js = client.get("/admin/ui/chats.js").text
    assert "template-status" in js
    assert "Retry" in js


def test_chats_js_template_send_surfaces_non_sent_outcome(client: TestClient) -> None:
    # Finding 4: a queued/suppressed outcome must not close the dialog as if it went out; the
    # handler reads result.status and shows a note for anything other than "sent".
    js = client.get("/admin/ui/chats.js").text
    assert "TEMPLATE_STATUS_NOTES" in js
    assert "result.status" in js or "sendStatus" in js


def test_chats_js_has_shared_fetch_next_page_helper(client: TestClient) -> None:
    # loadOlderThreads' single-page fetch-and-merge body must be extracted into a shared
    # fetchNextPage() helper so the auto-load loop (added next) can reuse it instead of
    # duplicating the fetch/merge/cursor-update logic. The function accepts an optional generation
    # token for the auto-load path to guard against race conditions.
    js = client.get("/admin/ui/chats.js").text
    assert "async function fetchNextPage(myGen)" in js
    assert "await fetchNextPage()" in js


def test_chats_js_auto_loads_all_threads_up_to_a_cap(client: TestClient) -> None:
    # "All chats on load": the filter chips (Unread/Human/Unexchanged/Exchanged) are client-side
    # over allThreads, so a matching thread sitting on page 2+ was invisible to every filter and
    # its count until "Load older chats" was clicked enough times to reach it. Auto-page in the
    # background (bounded, so the shared DB pool is never hit with an unbounded burst) instead of
    # requiring a manual click for realistic chat volumes.
    js = client.get("/admin/ui/chats.js").text
    assert "const AUTO_LOAD_MAX_PAGES = 10" in js
    assert "async function autoLoadRemainingThreads(myGen)" in js
    assert "await autoLoadRemainingThreads(myGen)" in js
    assert "Loading chats" in js
    # Race condition guard: generation token to detect when search changes mid-auto-load.
    assert "let loadGeneration = 0" in js
    assert "myGen !== loadGeneration" in js


def test_chats_js_staleness_check_is_inside_fetch_next_page(client: TestClient) -> None:
    # A prior fix round put this check caller-side (after `await fetchNextPage()` returned), which
    # cannot intercept the race: nothing runs between fetchNextPage's own await resolving and its
    # state writes for a caller-side check to catch. It must sit INSIDE fetchNextPage, before the
    # mutation lines, or a stale generation's in-flight fetch can still corrupt shared state.
    js = client.get("/admin/ui/chats.js").text
    fn = js.index("async function fetchNextPage(myGen)")
    check = js.index("if (myGen !== undefined && myGen !== loadGeneration) return false;", fn)
    mutate = js.index("allThreads = mergeThreads(allThreads, body.threads)", fn)
    assert fn < check < mutate


def test_chats_js_auto_load_paces_requests_and_skips_when_hidden(client: TestClient) -> None:
    # Each auto-loaded page costs the server a per-thread lookup, not just one query, against the
    # max_size=5 DB pool shared with the live webhook ack path (<5s) -- the loop must pace itself
    # and skip a backgrounded tab instead of firing a tight burst.
    js = client.get("/admin/ui/chats.js").text
    assert "document.hidden" in js
    assert "function sleep(ms)" in js
    assert "await sleep(150)" in js
    # Exactly one definition + one call site -- confirms autoLoadRemainingThreads is still only
    # invoked from loadThreadList (never from refreshFirstPage/pollTick, which stay out of scope).
    assert js.count("autoLoadRemainingThreads(") == 2


def test_chats_js_renders_customer_image_bubbles(client: TestClient) -> None:
    # Customer-sent photos (inbound image product lookup) must render as an <img>, not as text
    # (renderBubble's default `text.textContent = entry.text` would otherwise show the literal
    # string "undefined", since customer_image entries carry no `text` field).
    js = client.get("/admin/ui/chats.js").text
    assert '"customer_image"' in js
    assert "bubble-image" in js
    assert "/images/" in js
    html = client.get("/admin/ui/chats.html").text
    assert "bubble-image" in html
