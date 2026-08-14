-- Level 4 data model (architecture-plan v1.1). Idempotent.
CREATE TABLE IF NOT EXISTS app_config (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_mappings (
    order_gid                  text PRIMARY KEY,
    order_name                 text NOT NULL,
    order_number_int           bigint,
    phone_e164                 text,
    customer_name              text,
    email                      text,
    language                   text NOT NULL DEFAULT 'en',
    financial_status_at_create text,          -- creation-time SNAPSHOT, never authoritative
    is_cod                     boolean NOT NULL DEFAULT false,
    status                     text NOT NULL DEFAULT 'pending',
    store_id                   text NOT NULL DEFAULT 'thetavas',
    template_sent_at           timestamptz,
    responded_at               timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_mappings_phone ON order_mappings (phone_e164);
CREATE INDEX IF NOT EXISTS idx_order_mappings_name  ON order_mappings (order_name);
-- Supports the Q17 reminder sweep's WHERE status = 'template_sent' AND updated_at BETWEEN ...
-- filter (find_stale_template_sent). Additive + idempotent (CREATE INDEX IF NOT EXISTS), so it
-- needs no live ALTER — it is applied the same way every other index here is.
CREATE INDEX IF NOT EXISTS idx_order_mappings_status_updated ON order_mappings (status, updated_at);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id              bigserial PRIMARY KEY,
    dedupe_key      text NOT NULL UNIQUE,
    state           text NOT NULL DEFAULT 'queued',  -- queued|processing|sent|suppressed|failed|undeliverable
    kind            text NOT NULL,
    phone_e164      text NOT NULL,
    payload_json    text NOT NULL,
    template_wamid  text,
    delivery_status text,
    attempts        int NOT NULL DEFAULT 0,
    last_error_code text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbound_state ON outbound_messages (state, created_at);

CREATE TABLE IF NOT EXISTS processed_webhooks (
    webhook_id  text NOT NULL,
    topic       text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (webhook_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_processed_webhooks_received ON processed_webhooks (received_at);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  text PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          bigserial PRIMARY KEY,
    wa_id       text NOT NULL,
    order_gid   text NOT NULL,
    action      text NOT NULL,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_actions (
    id               bigserial PRIMARY KEY,
    order_gid        text NOT NULL,
    action           text NOT NULL,
    actor_wa_id      text,
    source_wamid     text,
    result           text NOT NULL,
    user_errors_json text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    running_summary text,
    paused_until    timestamptz,
    last_active_at  timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, last_active_at);

-- Tracks whether ONE AI handoff attempt has already been used in the current conversation
-- window (client decision, round 3 2026-08-06: one AI attempt, then immediate handoff on a
-- second request). Distinct from paused_until, which marks a human has already taken over.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS handoff_attempted_at timestamptz;

-- Enforces one conversation row per WhatsApp sender. Required so
-- PostgresConversationStore.get_or_create can use a single atomic
-- `INSERT ... ON CONFLICT (user_id) DO UPDATE` upsert instead of a SELECT-then-INSERT,
-- which would otherwise let two concurrent first-contact messages from the same sender
-- race and create two conversation rows. A unique index (not a named constraint) is used
-- because it is idempotent via IF NOT EXISTS, matching this file's existing index style,
-- and Postgres ON CONFLICT accepts any unique index on the target column(s).
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversations_user_id ON conversations (user_id);

CREATE TABLE IF NOT EXISTS messages (
    id              bigserial PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations (id),
    role            text NOT NULL,
    content         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_overrides (
    kind        text PRIMARY KEY,
    content     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customers (
    gid             text PRIMARY KEY,
    first_name      text,
    last_name       text,
    email           text,
    phone           text,
    address_line1   text,
    address_line2   text,
    city            text,
    state           text,
    postal_code     text,
    country         text,
    synced_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers (phone);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers (email);

CREATE TABLE IF NOT EXISTS orders (
    gid                    text PRIMARY KEY,
    name                   text NOT NULL,
    order_number           integer,
    customer_gid           text REFERENCES customers(gid) ON DELETE SET NULL,
    email                  text,
    phone                  text,
    shipping_phone         text,
    billing_phone          text,
    financial_status       text,
    fulfillment_status     text,
    cancelled_at           timestamptz,
    tags                   text[] NOT NULL DEFAULT '{}',
    payment_gateway_names  text[] NOT NULL DEFAULT '{}',
    total_amount           text,
    total_currency         text,
    customer_locale        text,
    order_created_at       timestamptz,
    synced_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders (phone);
CREATE INDEX IF NOT EXISTS idx_orders_shipping_phone ON orders (shipping_phone);
CREATE INDEX IF NOT EXISTS idx_orders_billing_phone ON orders (billing_phone);
CREATE INDEX IF NOT EXISTS idx_orders_name ON orders (name);
CREATE INDEX IF NOT EXISTS idx_orders_customer_gid ON orders (customer_gid);

CREATE TABLE IF NOT EXISTS order_items (
    id              bigserial PRIMARY KEY,
    order_gid       text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    title           text NOT NULL,
    sku             text,
    quantity        integer NOT NULL,
    variant_title   text,
    price_amount    text,
    price_currency  text
);
CREATE INDEX IF NOT EXISTS idx_order_items_order_gid ON order_items (order_gid);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items (sku);

-- Courier/tracking details Shopify sends after an order is fulfilled (FULFILLMENTS_CREATE /
-- FULFILLMENTS_UPDATE webhooks). One row per fulfillment; an order can have several (split
-- shipments), so this is a child table keyed by order_gid, mirroring order_items exactly
-- (FK to orders(gid) ON DELETE CASCADE, indexed by order_gid for the batched `= ANY` read).
-- Read-only Q&A enrichment: the order-tracking agent surfaces the tracking link when asked
-- (Q10). Additive + idempotent, applied the same way every other table here is.
CREATE TABLE IF NOT EXISTS fulfillments (
    gid              text PRIMARY KEY,
    order_gid        text NOT NULL REFERENCES orders(gid) ON DELETE CASCADE,
    status           text,
    tracking_company text,
    tracking_number  text,
    tracking_url     text,
    created_at       timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fulfillments_order_gid ON fulfillments (order_gid);

-- Shopify's own last-modified stamp for the mirrored resource (distinct from synced_at, which
-- is when WE wrote the row). Shopify does not guarantee webhook delivery order, so the mirror
-- upserts compare this value and refuse a write that is older than the stored one — otherwise a
-- late-arriving retry of an older orders/updated silently reverts fulfillment_status/cancelled_at
-- (permanently, for a terminal order with no further update coming). NULL = unknown, which is
-- always writable (backfill path / payloads without the field).
ALTER TABLE orders    ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS updated_at timestamptz;
