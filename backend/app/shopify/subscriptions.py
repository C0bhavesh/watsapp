# The Shopify webhook topics this app accepts on `/webhooks/shopify`.
#
# Webhook delivery is now driven by ADMIN-created webhook subscriptions (configured directly in
# Shopify's Settings -> Notifications -> Webhooks), NOT self-managed by this app. The receiver in
# app/channels/shopify_webhook.py derives its HANDLED_TOPICS set from this tuple, so the set of
# topics we accept and the set we handle can never drift. Admin-created subscriptions must be
# registered for exactly these topics.
#
# The former app-managed self-heal (`ensure_subscription` + webhookSubscriptionCreate/Update) was
# REMOVED on 2026-08-15 when webhook delivery was cut over to Admin-created webhooks: recreating an
# app-managed subscription would double-deliver every event (once to it, once to the Admin webhook).
#
# FULFILLMENTS_CREATE/UPDATE deliver courier/tracking data (needs the read_fulfillments scope).
REQUIRED_TOPICS: tuple[str, ...] = (
    "ORDERS_CREATE",
    "ORDERS_UPDATED",
    "CUSTOMERS_UPDATE",
    "FULFILLMENTS_CREATE",
    "FULFILLMENTS_UPDATE",
)
