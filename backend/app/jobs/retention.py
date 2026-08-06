"""DPDP retention purge — scheduled-job-ready, config-gated (ADR-005).

Reads ``retention_days`` from the operational controls. When it is 0 (the safe default)
this is a no-op: no automatic deletion happens until an owner sets a positive window —
the exact period is a pending client decision (Q15), so no policy is invented here.
When positive, it purges rows older than that many days from the erasure/retention tables.
"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.admin.controls import load_controls
from app.deps import Container


async def run_retention_purge(c: Container) -> dict[str, Any]:
    controls = await load_controls(c.config)
    days = controls.retention_days
    if days <= 0:
        return {"status": "disabled"}
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await c.ingest.purge_older_than(cutoff)
    return {"status": "purged", "retention_days": days, "deleted": asdict(result)}
