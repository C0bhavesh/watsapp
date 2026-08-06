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
