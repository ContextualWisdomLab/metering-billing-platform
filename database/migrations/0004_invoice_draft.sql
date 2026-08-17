BEGIN;

CREATE TABLE billing_core.invoice_draft (
    invoice_draft_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    rating_run_id uuid NOT NULL,
    usage_snapshot_hash text NOT NULL,
    currency_code text NOT NULL,
    invoice_draft_status text NOT NULL CHECK (invoice_draft_status IN ('draft')),
    drafted_total_amount numeric(38, 12) NOT NULL CHECK (drafted_total_amount >= 0),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, rating_run_id),
    UNIQUE (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, rating_run_id)
        REFERENCES billing_core.rating_run (tenant_account_id, rating_run_id),
    CHECK (usage_snapshot_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE billing_core.invoice_draft_line (
    invoice_draft_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    invoice_draft_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    billing_account_id uuid NOT NULL,
    meter_definition_id uuid NOT NULL REFERENCES billing_core.meter_definition (meter_definition_id),
    line_number integer NOT NULL CHECK (line_number > 0),
    rated_quantity numeric(38, 12) NOT NULL CHECK (rated_quantity >= 0),
    unit_price_amount numeric(38, 12) NOT NULL CHECK (unit_price_amount >= 0),
    line_total_amount numeric(38, 12) NOT NULL CHECK (line_total_amount >= 0),
    UNIQUE (invoice_draft_id, line_number),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id)
);

COMMIT;
