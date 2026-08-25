BEGIN;

CREATE TABLE billing_core.issued_credit_note (
    issued_credit_note_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    credit_adjustment_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    issued_invoice_id uuid,
    issued_credit_note_contract_version integer NOT NULL CHECK (issued_credit_note_contract_version >= 1),
    credit_adjustment_contract_version integer NOT NULL CHECK (credit_adjustment_contract_version >= 1),
    credit_reason_code text NOT NULL CHECK (credit_reason_code IN ('rating_correction', 'goodwill', 'billing_error')),
    credit_adjustment_source_payload_hash text NOT NULL,
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    tax_exclusive_amount numeric(38, 12) NOT NULL CHECK (tax_exclusive_amount >= 0),
    tax_amount numeric(38, 12) NOT NULL CHECK (tax_amount >= 0),
    tax_inclusive_amount numeric(38, 12) NOT NULL CHECK (tax_inclusive_amount >= 0),
    issued_credit_note_status text NOT NULL CHECK (issued_credit_note_status IN ('issued')),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, credit_adjustment_id),
    UNIQUE (tenant_account_id, issued_credit_note_id),
    FOREIGN KEY (tenant_account_id, credit_adjustment_id)
        REFERENCES billing_core.credit_adjustment (tenant_account_id, credit_adjustment_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, issued_invoice_id)
        REFERENCES billing_core.issued_invoice (tenant_account_id, issued_invoice_id),
    CHECK (credit_adjustment_source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (tax_inclusive_amount = tax_exclusive_amount + tax_amount)
);

COMMIT;
