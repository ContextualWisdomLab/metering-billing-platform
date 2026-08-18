BEGIN;

CREATE TABLE billing_core.spend_budget (
    spend_budget_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    billing_account_id uuid NOT NULL,
    spend_budget_contract_version integer NOT NULL CHECK (spend_budget_contract_version >= 1),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    budget_amount numeric(38, 12) NOT NULL CHECK (budget_amount > 0),
    window_started_at timestamptz NOT NULL,
    window_ended_at timestamptz NOT NULL,
    source_payload_hash text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        tenant_account_id,
        billing_account_id,
        window_started_at,
        window_ended_at,
        currency_code,
        source_payload_hash,
        spend_budget_contract_version
    ),
    UNIQUE (tenant_account_id, spend_budget_id),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id),
    CHECK (window_ended_at > window_started_at),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
