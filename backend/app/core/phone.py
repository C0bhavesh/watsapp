import re


def normalize_phone(raw: str | None) -> str | None:
    """Normalize to E.164. Default country: India (+91) for bare 10-digit numbers."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if 11 <= len(digits) <= 15:
        return f"+{digits}"
    return None
