BEGIN;

CREATE TABLE billing_core.rate_card (
    rate_card_id uuid PRIMARY KEY DEFAULT uuidv7(),
    rate_card_code text NOT NULL,
    rate_card_version integer NOT NULL CHECK (rate_card_version > 0),
    currency_code text NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    UNIQUE (rate_card_code, rate_card_version),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE billing_core.rate_card_price (
    rate_card_price_id uuid PRIMARY KEY DEFAULT uuidv7(),
    rate_card_id uuid NOT NULL REFERENCES billing_core.rate_card (rate_card_id),
    meter_code text NOT NULL,
    unit_price numeric(38, 12) NOT NULL CHECK (unit_price >= 0),
    UNIQUE (rate_card_id, meter_code)
);

CREATE TABLE billing_core.rating_run (
    rating_run_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    window_started_at timestamptz NOT NULL,
    window_ended_at timestamptz NOT NULL,
    rate_card_id uuid NOT NULL REFERENCES billing_core.rate_card (rate_card_id),
    rate_card_version integer NOT NULL CHECK (rate_card_version > 0),
    usage_snapshot_hash text NOT NULL CHECK (usage_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    currency_code text NOT NULL,
    invoice_intent_total numeric(38, 12) NOT NULL CHECK (invoice_intent_total >= 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, rating_run_id),
    UNIQUE (
        tenant_account_id,
        window_started_at,
        window_ended_at,
        rate_card_id,
        usage_snapshot_hash
    ),
    CHECK (window_ended_at > window_started_at)
);

CREATE TABLE billing_core.rating_line (
    rating_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL,
    rating_run_id uuid NOT NULL,
    meter_code text NOT NULL,
    billed_quantity numeric(38, 12) NOT NULL CHECK (billed_quantity >= 0),
    unit_price numeric(38, 12) NOT NULL CHECK (unit_price >= 0),
    line_amount numeric(38, 12) NOT NULL CHECK (line_amount >= 0),
    UNIQUE (tenant_account_id, rating_line_id),
    UNIQUE (rating_run_id, meter_code),
    FOREIGN KEY (tenant_account_id, rating_run_id)
        REFERENCES billing_core.rating_run (tenant_account_id, rating_run_id)
);

COMMIT;
