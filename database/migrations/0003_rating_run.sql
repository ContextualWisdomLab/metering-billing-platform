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
    meter_definition_id uuid NOT NULL REFERENCES billing_core.meter_definition (meter_definition_id),
    unit_price_amount numeric(38, 12) NOT NULL CHECK (unit_price_amount >= 0),
    UNIQUE (rate_card_id, meter_definition_id)
);

CREATE TABLE billing_core.rating_run (
    rating_run_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    rate_card_id uuid NOT NULL REFERENCES billing_core.rate_card (rate_card_id),
    window_started_at timestamptz NOT NULL,
    window_ended_at timestamptz NOT NULL,
    usage_snapshot_hash text NOT NULL,
    currency_code text NOT NULL,
    rated_total_amount numeric(38, 12) NOT NULL CHECK (rated_total_amount >= 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash),
    UNIQUE (tenant_account_id, rating_run_id),
    CHECK (window_ended_at > window_started_at),
    CHECK (usage_snapshot_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE billing_core.rating_line (
    rating_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    rating_run_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    billing_account_id uuid NOT NULL,
    meter_definition_id uuid NOT NULL REFERENCES billing_core.meter_definition (meter_definition_id),
    rated_quantity numeric(38, 12) NOT NULL CHECK (rated_quantity >= 0),
    unit_price_amount numeric(38, 12) NOT NULL CHECK (unit_price_amount >= 0),
    line_total_amount numeric(38, 12) NOT NULL CHECK (line_total_amount >= 0),
    UNIQUE (rating_run_id, billing_account_id, meter_definition_id),
    FOREIGN KEY (tenant_account_id, rating_run_id)
        REFERENCES billing_core.rating_run (tenant_account_id, rating_run_id),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id)
);

COMMIT;
