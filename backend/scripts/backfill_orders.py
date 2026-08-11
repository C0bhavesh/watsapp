"""One-time backfill: pull the last 12 months of Shopify orders into the local mirror
(customers/orders/order_items tables). Run once: python -m scripts.backfill_orders

Unlike apply_schema.py, this needs Shopify credentials (which require APP_MASTER_KEY to
decrypt), so it goes through the normal app wiring (get_container()) rather than a bare
DATABASE_URL connection.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.config.settings import Settings
from app.deps import get_container

BACKFILL_WINDOW_DAYS = 365


async def main() -> None:
    # get_container() silently falls back to the in-memory store when database_url is empty,
    # so an unguarded run would page a year of orders out of Shopify into a dict that dies with
    # the process and still print "backfill complete". Fail fast, as apply_schema.py does.
    if not Settings().database_url:  # type: ignore[call-arg]  # app_master_key comes from env
        raise SystemExit("DATABASE_URL is not set — refusing to backfill into an in-memory store")
    c = get_container()
    since = (datetime.now(UTC) - timedelta(days=BACKFILL_WINDOW_DAYS)).strftime("%Y-%m-%d")
    count = 0
    async for order in c.shopify.list_orders_created_since(since):
        await c.ingest.upsert_order_mirror(order)
        count += 1
        if count % 50 == 0:
            print(f"backfilled {count} orders so far...")
    print(f"backfill complete: {count} orders synced (created since {since})")


if __name__ == "__main__":
    asyncio.run(main())
