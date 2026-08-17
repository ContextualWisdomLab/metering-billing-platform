BEGIN;

ALTER TABLE billing_core.rate_card
    ADD COLUMN tenant_account_id uuid REFERENCES billing_core.tenant_account (tenant_account_id);

ALTER TABLE billing_core.rate_card
    ADD COLUMN rate_card_name text;

ALTER TABLE billing_core.rate_card
    ADD CONSTRAINT rate_card_name_snake_case
        CHECK (rate_card_name IS NULL OR rate_card_name ~ '^[a-z][a-z0-9]*_[a-z0-9_]+$');

ALTER TABLE billing_core.rate_card
    ADD CONSTRAINT rate_card_tenant_identity
        UNIQUE (tenant_account_id, rate_card_id);

CREATE UNIQUE INDEX rate_card_tenant_name
    ON billing_core.rate_card (tenant_account_id, rate_card_name)
    WHERE tenant_account_id IS NOT NULL AND rate_card_name IS NOT NULL;

CREATE TABLE billing_core.rate_card_version (
    rate_card_version_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    rate_card_id uuid NOT NULL,
    version_number integer NOT NULL CHECK (version_number >= 1),
    rate_card_contract_version integer NOT NULL CHECK (rate_card_contract_version >= 1),
    currency_code text NOT NULL,
    source_payload_hash text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, rate_card_id, version_number),
    UNIQUE (tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version),
    UNIQUE (tenant_account_id, rate_card_version_id),
    FOREIGN KEY (tenant_account_id, rate_card_id)
        REFERENCES billing_core.rate_card (tenant_account_id, rate_card_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE billing_core.rate_card_line (
    rate_card_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    rate_card_version_id uuid NOT NULL,
    metric_code text NOT NULL CHECK (metric_code ~ '^[a-z][a-z0-9]*_[a-z0-9_]+$'),
    unit_amount numeric(38, 12) NOT NULL CHECK (unit_amount > 0),
    currency_code text NOT NULL,
    UNIQUE (tenant_account_id, rate_card_version_id, metric_code),
    UNIQUE (tenant_account_id, rate_card_line_id),
    FOREIGN KEY (tenant_account_id, rate_card_version_id)
        REFERENCES billing_core.rate_card_version (tenant_account_id, rate_card_version_id)
);

COMMIT;
