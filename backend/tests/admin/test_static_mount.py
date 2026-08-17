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
