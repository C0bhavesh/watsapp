from datetime import UTC, datetime, timedelta

import pytest

from app.admin.auth import check_password, issue_token, verify_token

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def test_token_roundtrip() -> None:
    t = issue_token("k1", NOW)
    assert verify_token("k1", t, NOW) is True


def test_token_expires_after_ttl() -> None:
    t = issue_token("k1", NOW, ttl_hours=12)
    assert verify_token("k1", t, NOW + timedelta(hours=12, seconds=1)) is False


def test_token_rejects_tamper() -> None:
    t = issue_token("k1", NOW)
    payload, sig = t.rsplit(".", 1)
    assert verify_token("k1", f"{int(payload) + 9999}.{sig}", NOW) is False


def test_token_rejects_wrong_secret() -> None:
    assert verify_token("other", issue_token("k1", NOW), NOW) is False


def test_verify_empty_secret_fails_closed() -> None:
    assert verify_token("", issue_token("k1", NOW), NOW) is False


def test_verify_malformed_token() -> None:
    assert verify_token("k1", "not-a-token", NOW) is False
    assert verify_token("k1", "abc.def", NOW) is False


def test_verify_non_ascii_signature_fails_closed() -> None:
    # The signature is split from the attacker-controlled cookie; a non-ASCII byte must
    # NOT raise TypeError inside hmac.compare_digest — fail closed (return False) instead.
    assert verify_token("k1", "9999999999.sigé", NOW) is False


def test_verify_non_ascii_payload_fails_closed() -> None:
    # A non-ASCII payload segment must likewise never raise.
    assert verify_token("k1", "99é99.sig", NOW) is False
    assert verify_token("k1", "éé.éé", NOW) is False


def test_issue_empty_secret_raises() -> None:
    with pytest.raises(ValueError):
        issue_token("", NOW)


def test_check_password() -> None:
    assert check_password("a", "a") is True
    assert check_password("a", "b") is False
    assert check_password("anything", "") is False  # unset password never grants access
