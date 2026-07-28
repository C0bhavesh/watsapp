import pytest

from app.channels.copy import SUPPORTED_LANGUAGES, copy_for

KEYS = (
    "order_confirmed",
    "cancel_confirm_prompt",
    "order_cancelled",
    "order_not_found",
    "refusal_other_order",
    "error_fallback",
)


@pytest.mark.parametrize("key", KEYS)
def test_every_key_has_every_language(key: str) -> None:
    for language in SUPPORTED_LANGUAGES:
        text = copy_for(key, language)
        assert isinstance(text, str) and text.strip()


def test_unsupported_language_falls_back_to_english() -> None:
    assert copy_for("order_confirmed", "ta") == copy_for("order_confirmed", "en")


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        copy_for("no_such_key", "en")


def test_no_emojis_in_any_copy() -> None:
    for key in KEYS:
        for language in SUPPORTED_LANGUAGES:
            text = copy_for(key, language)
            assert all(ord(ch) < 0x1F000 for ch in text), (key, language)
