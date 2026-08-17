BEGIN;

CREATE TABLE billing_core.webhook_subscription (
    webhook_subscription_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    webhook_subscription_contract_version integer NOT NULL CHECK (webhook_subscription_contract_version >= 1),
    callback_url text NOT NULL,
    event_type_set text NOT NULL,
    webhook_secret_prefix text NOT NULL,
    webhook_secret_hash text NOT NULL CHECK (webhook_secret_hash ~ '^hmac-sha256:[0-9a-f]{64}$'),
    subscription_status text NOT NULL CHECK (subscription_status IN ('active', 'revoked')),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz,
    UNIQUE (tenant_account_id, webhook_subscription_id),
    UNIQUE (tenant_account_id, callback_url, event_type_set, webhook_subscription_contract_version),
    UNIQUE (webhook_secret_hash),
    CHECK (revoked_at IS NULL OR revoked_at >= issued_at)
);

CREATE TABLE billing_core.webhook_outbox_event (
    outbox_event_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    event_type_code text NOT NULL,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    source_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    delivery_status text NOT NULL CHECK (delivery_status IN ('pending', 'delivered')),
    payload_json text NOT NULL,
    enqueued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, outbox_event_id),
    UNIQUE (tenant_account_id, event_type_code, source_id, payload_hash)
);

CREATE TABLE billing_core.webhook_delivery_attempt (
    delivery_attempt_id uuid PRIMARY KEY DEFAULT uuidv7(),
    outbox_event_id uuid NOT NULL,
    webhook_subscription_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    attempt_number integer NOT NULL CHECK (attempt_number >= 1),
    http_status integer,
    delivered_at timestamptz,
    failure_reason_code text,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, delivery_attempt_id),
    UNIQUE (outbox_event_id, webhook_subscription_id, attempt_number),
    FOREIGN KEY (tenant_account_id, outbox_event_id)
        REFERENCES billing_core.webhook_outbox_event (tenant_account_id, outbox_event_id),
    FOREIGN KEY (tenant_account_id, webhook_subscription_id)
        REFERENCES billing_core.webhook_subscription (tenant_account_id, webhook_subscription_id),
    CHECK (
        (delivered_at IS NOT NULL AND failure_reason_code IS NULL)
        OR (delivered_at IS NULL AND failure_reason_code IS NOT NULL)
    )
);

COMMIT;
