"""Send-policy decision helper — the ADR-002 kill switch, in one pure function.

Maps ``send_mode`` (off/shadow/allowlist/live) + the allowlist to a ``send``/``suppress``
decision for a single phone. Never raises: any unrecognized mode fails SAFE to ``suppress``.
The outbox drain treats ``off`` specially (it returns before looping at all), but this helper
still maps ``off`` -> ``suppress`` so it is safe to call in every mode.
"""

from app.core.phone import normalize_phone


def send_decision(send_mode: str, allowlist: list[str], phone: str) -> str:
    """Return ``"send"`` or ``"suppress"`` for one phone under the given send mode."""
    if send_mode == "live":
        return "send"
    if send_mode == "allowlist":
        target = normalize_phone(phone)
        if target is None:
            return "suppress"
        normalized = {normalize_phone(p) for p in allowlist}
        return "send" if target in normalized else "suppress"
    # off, shadow, and any unknown/misconfigured mode all fail safe to suppress.
    return "suppress"
