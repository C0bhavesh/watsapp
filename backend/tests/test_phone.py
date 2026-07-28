import pytest

from app.core.phone import normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919664290413", "+919664290413"),
        ("+91 96642 90413", "+919664290413"),
        ("9664290413", "+919664290413"),
        ("09664290413", "+919664290413"),
        ("919664290413", "+919664290413"),
        ("917575072795", "+917575072795"),        # wa_id form
        ("0091 9664290413", "+919664290413"),
        ("+1 555 651 8147", "+15556518147"),      # non-Indian stays as-is
        ("", None),
        (None, None),
        ("hello", None),
        ("123", None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


def test_non_str_phone_is_none_not_typeerror() -> None:
    # Shopify may send phone as a bare int (919664290413) on a signed payload.
    assert normalize_phone(919664290413) is None  # type: ignore[arg-type]
