"""Actor-free admin audit logging (item 4): login + credential/knowledge/controls/erasure.

Actor-free because there is one shared ADMIN_PASSWORD — no per-user identity to log. Log lines
carry action/resource/outcome only, NEVER a value (passwords, keys, credential contents,
knowledge bodies, or the erased phone number).
"""

import asyncio
import hashlib
import hmac
import logging

import pytest
from fastapi.testclient import TestClient

from app.deps import get_container
from app.store.base import MappingUpsert, OutboundDraft

PHONE = "+919111111111"


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_login_success_and_failure_audited(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.admin"):
        client.post("/admin/login", json={"password": "wrong-password"})
        client.post("/admin/login", json={"password": "test-admin-pass"})
    text = caplog.text
    assert "admin_audit action=login outcome=failure" in text
    assert "admin_audit action=login outcome=success" in text
    # The submitted passwords must never appear in a log line.
    assert "wrong-password" not in text
    assert "test-admin-pass" not in text


def test_credential_change_audited_without_value(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    login(client)
    with caplog.at_level(logging.INFO, logger="app.admin"):
        r = client.post(
            "/admin/shopify",
            json={"client_id": "the-id", "client_secret": "the-secret-value"},
        )
    assert r.status_code == 200, r.text
    assert "admin_audit action=credential_set resource=shopify outcome=success" in caplog.text
    assert "the-secret-value" not in caplog.text


def test_controls_change_audited(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    with caplog.at_level(logging.INFO, logger="app.admin"):
        assert client.put("/admin/controls", json=base).status_code == 200
    assert "admin_audit action=controls_set resource=controls outcome=success" in caplog.text


def test_knowledge_change_audited(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    login(client)
    with caplog.at_level(logging.INFO, logger="app.admin"):
        r = client.put("/admin/knowledge/brand_voice", json={"content": "Be kind."})
    assert r.status_code == 200, r.text
    assert (
        "admin_audit action=knowledge_set resource=knowledge:brand_voice outcome=success"
        in caplog.text
    )
    assert "Be kind." not in caplog.text


def test_erasure_audited_without_phone(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    login(client)
    mapping = MappingUpsert(
        order_gid="gid://shopify/Order/1", order_name="tavas1", order_number_int=1,
        phone_e164=PHONE, customer_name="T", email="t@e.c", language="en",
        financial_status_at_create="pending", is_cod=True,
    )
    outbound = OutboundDraft(
        dedupe_key="order_created:gid://shopify/Order/1", kind="order_confirmation",
        phone_e164=PHONE, payload_json="{}",
    )
    asyncio.run(
        get_container().ingest.ingest_order_created(
            "wh-1", "orders/create", mapping, outbound
        )
    )
    with caplog.at_level(logging.INFO, logger="app.admin"):
        r = client.post("/admin/erasure", json={"phone": PHONE})
    assert r.status_code == 200
    # The resource is an HMAC fingerprint of the phone (recomputable from the number + the
    # app master key), NOT the literal "phone" and NEVER the number itself.
    key = get_container().settings.app_master_key
    fingerprint = hmac.new(key.encode(), PHONE.encode(), hashlib.sha256).hexdigest()[:16]
    assert (
        f"admin_audit action=erasure resource=phone:{fingerprint} outcome=success"
        in caplog.text
    )
    # The blast radius (row count) is logged so a reviewer needn't touch the response body.
    assert "deleted=2" in caplog.text
    # The erased phone number is PII and must never appear in the audit log.
    assert PHONE not in caplog.text
    # The old, non-attributable literal must be gone.
    assert "resource=phone outcome" not in caplog.text


def test_unauthorized_admin_access_audited(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A forged/absent cookie against any /admin route must leave an authz-denied audit line
    # naming the route path — 50 forged attempts should not be invisible.
    with caplog.at_level(logging.INFO, logger="app.admin"):
        r = client.get("/admin/session")
    assert r.status_code == 401
    assert "admin_audit action=authz resource=/admin/session outcome=denied" in caplog.text
