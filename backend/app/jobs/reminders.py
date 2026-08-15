"""Order-confirmation reminder sweep (Flow A, Q17).

If a customer received the ``cod_confirmation`` template and did not tap Confirm or Cancel
within 1 hour, resend the EXACT same template ONCE as a reminder — and never again for that order.

Design (reuses the existing outbox pipeline, adds no parallel machinery):
- Eligibility = a mapping still at status ``template_sent`` whose template was SENT between 1 and
  24 hours ago. The window is anchored on ``order_mappings.updated_at`` (the moment the drain
  stamped ``template_sent`` via ``set_mapping_status``), NOT ``created_at`` (webhook-ingest time) —
  otherwise a backlog flushed hours after ingest would be reminded the instant its template sends.
  The 24-hour ceiling stops a historically-unanswered order (mappings are kept indefinitely, client
  Q15) from being mass-reminded on the first run after this feature deploys. A Confirm/Cancel tap
  moves the status off ``template_sent`` (``core.order_actions``), so a tapped order is excluded
  automatically — no separate cancellation flag needed.
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
# 24-hour ceiling: never remind an order more than a day past its template-sent moment. Bounds the
# sweep so a mass of historically-unanswered mappings can't be reminder-blasted on first run.
_REMINDER_CEILING_MINUTES = 24 * 60

_ORIGINAL_PREFIX = "order_created:"
_REMINDER_PREFIX = "order_reminder:"


async def run_send_reminders(c: Container) -> dict[str, object]:
    stale = await c.ingest.find_stale_template_sent(
        older_than_minutes=_REMINDER_THRESHOLD_MINUTES,
        max_age_minutes=_REMINDER_CEILING_MINUTES,
    )
    # Fetch every original push payload in ONE batched round-trip, not one query per stale row.
    originals = await c.ingest.find_outbound_by_dedupe_keys(
        [f"{_ORIGINAL_PREFIX}{m.order_gid}" for m in stale]
    )
    queued = 0
    for mapping in stale:
        gid = mapping.order_gid
        original = originals.get(f"{_ORIGINAL_PREFIX}{gid}")
        if original is None:
            # The original push row is gone (e.g. erased) — nothing to reuse; skip rather than
            # fabricate a payload.
            continue
        reminder = replace(original, dedupe_key=f"{_REMINDER_PREFIX}{gid}")
        if await c.ingest.enqueue_outbound(reminder):
            queued += 1
    return {"swept": len(stale), "queued": queued}
