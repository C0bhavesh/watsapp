"""DPDP retention purge — scheduled-job-ready, config-gated (ADR-005).

Reads ``retention_days`` from the operational controls. When it is 0 (the safe default)
this is a no-op: no automatic deletion happens until an owner sets a positive window.
When positive, it ages out ONLY the deletable tables older than that many days:
conversation history, temporary AI context, action/pending rows, and processed-message logs.

Customer/order data (``order_mappings``/``outbound_messages``) is NEVER touched by this job —
the client decided (round 3, 2026-08-06, client-decisions-all.md Q15) it is kept INDEFINITELY;
``purge_older_than`` excludes those tables. Only on-demand right-to-erasure
(POST /admin/erasure -> ``delete_by_phone``) may remove a specific customer's order data.
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
