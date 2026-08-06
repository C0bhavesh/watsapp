from app.core.sanitize import strip_markdown


def test_double_asterisk_becomes_whatsapp_single_asterisk() -> None:
    assert strip_markdown("This is **bold** text") == "This is *bold* text"


def test_heading_marker_removed() -> None:
    assert strip_markdown("# Order status\nShipped") == "Order status\nShipped"


def test_multiple_blank_lines_collapsed() -> None:
    assert strip_markdown("line one\n\n\n\nline two") == "line one\n\nline two"


def test_plain_text_unchanged() -> None:
    text = "Hello, your order is on the way."
    assert strip_markdown(text) == text


def test_leading_trailing_whitespace_stripped() -> None:
    assert strip_markdown("  hello  ") == "hello"


def test_multiple_bold_spans_all_converted() -> None:
    assert strip_markdown("**one** and **two**") == "*one* and *two*"
