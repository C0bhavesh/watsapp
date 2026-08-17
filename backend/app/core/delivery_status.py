_RANK: dict[str, int] = {"sent": 0, "delivered": 1, "read": 2}


def should_apply_delivery_status(current: str | None, new: str) -> bool:
    """True if `new` should overwrite `current` per the delivery/read ordering guard.

    sent < delivered < read strictly increases; a lower-or-equal rank never overwrites a
    higher one (protects against out-of-order webhook delivery -- a late "delivered" arriving
    after "read" is already recorded must not regress the stored state). `failed` is WhatsApp's
    own definitive "this did not go through" signal: it always applies going forward, but once
    recorded is terminal -- nothing overwrites a `failed` row after the fact. An unrecognized
    `new` value (a future Meta status this app doesn't know about yet) is rejected, never applied.
    """
    if current == "failed":
        return False
    if new == "failed":
        return True
    if new not in _RANK:
        return False
    if current is None:
        return True
    return _RANK.get(current, -1) < _RANK[new]
