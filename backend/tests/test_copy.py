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

# Phase 5 deterministic button-tap replies (confirm/cancel dispatch).
PHASE5_KEYS = (
    "confirm_success",
    "already_confirmed",
    "cancel_are_you_sure",
    "cancel_yes_title",
    "cancel_no_title",
    "cancel_requested",
    "cancel_kept",
    "cancel_too_late",
    "already_cancelled",
    "cancel_failed",
    "not_found",
)

# Reply-button titles Meta caps at 20 chars (send_buttons rejects longer).
BUTTON_TITLE_KEYS = ("cancel_yes_title", "cancel_no_title")

ALL_KEYS = KEYS + PHASE5_KEYS


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_key_has_every_language(key: str) -> None:
    for language in SUPPORTED_LANGUAGES:
        text = copy_for(key, language)
        assert isinstance(text, str) and text.strip()


@pytest.mark.parametrize("key", BUTTON_TITLE_KEYS)
def test_button_titles_within_meta_cap(key: str) -> None:
    for language in SUPPORTED_LANGUAGES:
        assert len(copy_for(key, language)) <= 20, (key, language)


def test_unsupported_language_falls_back_to_english() -> None:
    assert copy_for("order_confirmed", "ta") == copy_for("order_confirmed", "en")


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        copy_for("no_such_key", "en")


def test_no_emojis_in_any_copy() -> None:
    for key in ALL_KEYS:
        for language in SUPPORTED_LANGUAGES:
            text = copy_for(key, language)
            assert all(ord(ch) < 0x1F000 for ch in text), (key, language)
