"""Order-confirmation reminder sweep (Flow A, Q17).

If a customer received the ``order_confirmation_cod`` template and did not tap Confirm or Cancel
within 1 hour, resend the EXACT same template ONCE as a reminder — and never again for that order.

Design (reuses the existing outbox pipeline, adds no parallel machinery):
- Eligibility = a mapping still at status ``template_sent`` whose ``created_at`` is older than the
  threshold. A Confirm/Cancel tap moves the status off ``template_sent`` (``core.order_actions``),
  so a tapped order is excluded automatically — no separate cancellation flag needed.
- The reminder reuses the ORIGINAL push's queued payload verbatim (``order_created:{gid}``), never
  a fresh Shopify fetch.
- Exactly-once is the SAME UNIQUE ``dedupe_key`` guarantee the whole outbox relies on: the reminder
  is queued under ``order_reminder:{gid}`` with ``ON CONFLICT DO NOTHING``, so this sweep can run
  every tick (or overlap) and still queue at most one reminder per order — no "already reminded"
  flag. Once queued the row flows through the EXISTING atomic-claim -> ``send_one_outbound``
  pipeline unchanged; this job only QUEUES and never sends, so the ``send_mode`` kill switch stays
  enforced in one place (the drain).
"""

from dataclasses import replace

from app.deps import Container

# 1 hour, per the owner-directed Q17 behaviour. A single reminder, then never again.
_REMINDER_THRESHOLD_MINUTES = 60

_ORIGINAL_PREFIX = "order_created:"
_REMINDER_PREFIX = "order_reminder:"


async def run_send_reminders(c: Container) -> dict[str, object]:
    stale = await c.ingest.find_stale_template_sent(
        older_than_minutes=_REMINDER_THRESHOLD_MINUTES
    )
    queued = 0
    for mapping in stale:
        gid = mapping.order_gid
        original = await c.ingest.find_outbound_by_dedupe_key(f"{_ORIGINAL_PREFIX}{gid}")
        if original is None:
            # The original push row is gone (e.g. erased) — nothing to reuse; skip rather than
            # fabricate a payload.
            continue
        reminder = replace(original, dedupe_key=f"{_REMINDER_PREFIX}{gid}")
        if await c.ingest.enqueue_outbound(reminder):
            queued += 1
    return {"swept": len(stale), "queued": queued}
