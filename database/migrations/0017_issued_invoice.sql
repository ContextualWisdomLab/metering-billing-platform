BEGIN;

CREATE TABLE billing_core.issued_invoice (
    issued_invoice_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    invoice_draft_id uuid NOT NULL,
    issued_invoice_contract_version integer NOT NULL CHECK (issued_invoice_contract_version >= 1),
    rating_run_id uuid NOT NULL,
    usage_snapshot_hash text NOT NULL,
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    tax_exclusive_amount numeric(38, 12) NOT NULL CHECK (tax_exclusive_amount >= 0),
    tax_amount numeric(38, 12) NOT NULL CHECK (tax_amount >= 0),
    tax_inclusive_amount numeric(38, 12) NOT NULL CHECK (tax_inclusive_amount >= 0),
    issued_invoice_status text NOT NULL CHECK (issued_invoice_status IN ('issued')),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    due_at timestamptz,
    UNIQUE (tenant_account_id, invoice_draft_id),
    UNIQUE (tenant_account_id, issued_invoice_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    CHECK (usage_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (tax_inclusive_amount = tax_exclusive_amount + tax_amount)
);

CREATE TABLE billing_core.issued_invoice_line (
    issued_invoice_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    issued_invoice_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number > 0),
    billing_account_reference text NOT NULL,
    meter_code text NOT NULL,
    unit_code text NOT NULL,
    rated_quantity numeric(38, 12) NOT NULL CHECK (rated_quantity >= 0),
    unit_price_amount numeric(38, 12) NOT NULL CHECK (unit_price_amount >= 0),
    line_total_amount numeric(38, 12) NOT NULL CHECK (line_total_amount >= 0),
    UNIQUE (issued_invoice_id, line_number),
    FOREIGN KEY (tenant_account_id, issued_invoice_id)
        REFERENCES billing_core.issued_invoice (tenant_account_id, issued_invoice_id)
);

COMMIT;
