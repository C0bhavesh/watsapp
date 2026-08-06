import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Convert standard Markdown to WhatsApp-safe plain text.

    WhatsApp renders ``**bold**`` literally (its own bold marker is a single ``*``), so an
    LLM reply written in standard Markdown must be converted, not just stripped. Heading
    markers are removed (WhatsApp has no heading concept); runs of 3+ blank lines are
    collapsed to keep replies compact on a phone screen.
    """
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _HEADING_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
