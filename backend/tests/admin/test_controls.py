import asyncio
import json

from fastapi.testclient import TestClient

from app.deps import get_container


def login(client: TestClient) -> None:
    r = client.post("/admin/login", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text


def test_defaults(client: TestClient) -> None:
    login(client)
    r = client.get("/admin/controls")
    assert r.status_code == 200
    body = r.json()
    assert body["send_mode"] == "off"
    assert body["push_policy"] == "cod_only"
    assert body["reveal_fields"] == ["order_number", "email", "status", "items", "tracking"]
    assert body["default_language"] == "en"
    assert body["push_staleness_hours"] == 6
    # DPDP retention defaults to 0 = disabled (no policy invented until the client confirms Q15).
    assert body["retention_days"] == 0


def test_retention_days_roundtrip_and_validation(client: TestClient) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    assert client.put("/admin/controls", json={**base, "retention_days": 365}).status_code == 200
    assert client.get("/admin/controls").json()["retention_days"] == 365
    # Negative or absurd values are rejected.
    assert client.put("/admin/controls", json={**base, "retention_days": -1}).status_code == 422
    assert (
        client.put("/admin/controls", json={**base, "retention_days": 40000}).status_code == 422
    )


def test_put_roundtrip(client: TestClient) -> None:
    login(client)
    doc = {
        "send_mode": "allowlist",
        "allowlist_phones": ["+919664290413"],
        "push_policy": "all",
        "reveal_fields": ["order_number", "status"],
        "tags": {
            "pending": ["COD pending"],
            "confirmed": ["confirmed", "Confirmed by wati"],
            "cancelled": ["cancelled", "Cancel by wati"],
            "cancel_requested": ["bot-cancel-requested"],
        },
        "default_language": "hi",
        "push_staleness_hours": 12,
        "retention_days": 180,
        "vip_order_count_threshold": 5,
        "public_base_url": "https://bot.example.com",
        "owner_alert_number": "",
    }
    assert client.put("/admin/controls", json=doc).status_code == 200
    assert client.get("/admin/controls").json() == doc


def test_put_rejects_bad_values(client: TestClient) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    assert client.put("/admin/controls", json={**base, "send_mode": "on"}).status_code == 422
    assert (
        client.put("/admin/controls", json={**base, "allowlist_phones": ["12345"]}).status_code
        == 422
    )
    assert (
        client.put("/admin/controls", json={**base, "reveal_fields": ["phone"]}).status_code
        == 422
    )
    assert (
        client.put(
            "/admin/controls", json={**base, "public_base_url": "http://insecure.example"}
        ).status_code
        == 422
    )


def test_over_long_tag_rejected(client: TestClient) -> None:
    login(client)
    doc = {"tags": {"pending": ["x" * 100], "confirmed": [], "cancelled": []}}
    # Shopify caps tags at 255 chars; our per-tag cap rejects an over-long tag before it
    # could fail at runtime inside tagsAdd.
    assert client.put("/admin/controls", json=doc).status_code == 422


def test_corrupt_unicode_int_value_falls_back_not_500(client: TestClient) -> None:
    login(client)

    async def _seed_bad_int() -> None:
        # Superscript two: str.isdigit() is True but int() raises ValueError. The old
        # isdigit() gate would pass it through to int() and 500 the panel; int()-guard degrades.
        await get_container().config_repo.set("retention_days", "²")

    asyncio.run(_seed_bad_int())
    r = client.get("/admin/controls")
    assert r.status_code == 200
    assert r.json()["retention_days"] == 0  # falls back to the safe default


def test_unicode_digits_rejected_in_e164_fields(client: TestClient) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    # Arabic-Indic digits satisfy a Unicode-aware \\d but are not a real phone number — the
    # ASCII-only pattern must reject them for allowlist_phones and owner_alert_number.
    arabic = "+٩٩٩٩٩٩٩٩٩٩"
    assert (
        client.put("/admin/controls", json={**base, "allowlist_phones": [arabic]}).status_code
        == 422
    )
    assert (
        client.put("/admin/controls", json={**base, "owner_alert_number": arabic}).status_code
        == 422
    )


def test_schema_invalid_stored_value_returns_defaults_not_500(client: TestClient) -> None:
    login(client)

    async def _seed_bad() -> None:
        # Valid JSON but fails the model (some other writer path) — GET must not 500.
        await get_container().config_repo.set("reveal_fields", json.dumps(["phone"]))

    asyncio.run(_seed_bad())
    r = client.get("/admin/controls")
    assert r.status_code == 200
    assert r.json()["reveal_fields"] == ["order_number", "email", "status", "items", "tracking"]


def test_unicode_digit_int_value_falls_back_to_safe_default(client: TestClient) -> None:
    login(client)

    async def _seed_arabic_int() -> None:
        # Arabic-Indic digits: int("٣٠") == 30, so a bare int() gate would silently enable a
        # 30-day retention purge from a corrupted stored value. The ASCII-digit gate rejects it,
        # falling back to the safe disabled default (0) instead of an unintended purge period.
        await get_container().config_repo.set("retention_days", "٣٠")

    asyncio.run(_seed_arabic_int())
    r = client.get("/admin/controls")
    assert r.status_code == 200
    assert r.json()["retention_days"] == 0


def test_one_corrupt_field_does_not_reset_the_whole_document(client: TestClient) -> None:
    login(client)

    async def _seed() -> None:
        repo = get_container().config_repo
        # A deliberately narrowed reveal_fields (hiding email/status) is a real admin choice.
        await repo.set("reveal_fields", json.dumps(["order_number"]))
        # An UNRELATED field is corrupted out of range (le=3650). It must NOT drag the whole
        # document back to defaults and silently re-widen reveal_fields (PII over-disclosure).
        await repo.set("retention_days", "40000")

    asyncio.run(_seed())
    body = client.get("/admin/controls").json()
    # The corrupt field falls back to its OWN default...
    assert body["retention_days"] == 0
    # ...while the valid, intentionally-narrowed field is preserved.
    assert body["reveal_fields"] == ["order_number"]


def test_trailing_newline_phone_rejected(client: TestClient) -> None:
    login(client)
    base = client.get("/admin/controls").json()
    # Python's re `$` matches before a trailing newline; `\\Z` does not. A phone with a trailing
    # newline must be rejected outright, not silently accepted then failing to compare elsewhere.
    assert (
        client.put(
            "/admin/controls", json={**base, "owner_alert_number": "+919111111111\n"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/admin/controls", json={**base, "allowlist_phones": ["+919111111111\n"]}
        ).status_code
        == 422
    )


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/controls").status_code == 401


def test_defaults_include_vip_threshold() -> None:
    from app.admin.controls import AdminControls

    assert AdminControls().vip_order_count_threshold == 3


def test_items_is_a_valid_reveal_field() -> None:
    from app.admin.controls import AdminControls

    # "items" reverses the earlier "items/amounts stay hidden" decision -- must no longer be
    # rejected as an unknown reveal field.
    assert AdminControls(reveal_fields=["items"]).reveal_fields == ["items"]


def test_default_reveal_fields_includes_items() -> None:
    from app.admin.controls import AdminControls

    assert "items" in AdminControls().reveal_fields


def test_tracking_is_an_accepted_reveal_field_and_default_enabled() -> None:
    # Q10 (already answered): the bot may share the Shopify tracking link when asked --
    # pre-approved, so "tracking" is a valid reveal field, default-enabled, admin-toggleable.
    from app.admin.controls import REVEAL_ALLOWED, AdminControls

    assert "tracking" in REVEAL_ALLOWED
    assert "tracking" in AdminControls().reveal_fields
    assert AdminControls(reveal_fields=["tracking"]).reveal_fields == ["tracking"]


async def test_vip_threshold_roundtrip(master_key: str) -> None:
    from app.admin.controls import AdminControls, load_controls, save_controls
    from app.config.crypto import SecretVault
    from app.config.service import ConfigService
    from app.store.memory import InMemoryConfigRepo

    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await save_controls(config, AdminControls(vip_order_count_threshold=5))
    loaded = await load_controls(config)
    assert loaded.vip_order_count_threshold == 5
