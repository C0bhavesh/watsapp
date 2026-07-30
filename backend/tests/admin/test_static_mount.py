from fastapi.testclient import TestClient


def test_static_panel_served(client: TestClient) -> None:
    r = client.get("/admin/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Thetavas Admin" in r.text
