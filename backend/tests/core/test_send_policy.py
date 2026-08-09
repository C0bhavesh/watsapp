from app.core.send_policy import send_decision


def test_off_suppresses() -> None:
    assert send_decision("off", [], "+919664290413") == "suppress"


def test_shadow_suppresses() -> None:
    assert send_decision("shadow", ["+919664290413"], "+919664290413") == "suppress"


def test_live_sends() -> None:
    assert send_decision("live", [], "+919664290413") == "send"


def test_allowlist_hit_sends() -> None:
    assert send_decision("allowlist", ["+919664290413"], "+919664290413") == "send"


def test_allowlist_hit_normalizes_both_sides() -> None:
    # A bare 10-digit allowlist entry / phone both normalize to +91..., so they match.
    assert send_decision("allowlist", ["9664290413"], "919664290413") == "send"


def test_allowlist_miss_suppresses() -> None:
    assert send_decision("allowlist", ["+911111111111"], "+919664290413") == "suppress"


def test_allowlist_unparseable_phone_suppresses() -> None:
    assert send_decision("allowlist", ["+919664290413"], "not-a-number") == "suppress"


def test_unknown_mode_suppresses() -> None:
    assert send_decision("banana", [], "+919664290413") == "suppress"
