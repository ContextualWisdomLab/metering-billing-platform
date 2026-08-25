BEGIN;

CREATE TABLE billing_core.unapplied_cash_application (
    unapplied_cash_application_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    unapplied_cash_id uuid NOT NULL,
    collection_case_id uuid NOT NULL,
    payment_receipt_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    unapplied_cash_application_contract_version integer NOT NULL CHECK (
        unapplied_cash_application_contract_version >= 1
    ),
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    applied_amount numeric(38, 12) NOT NULL CHECK (applied_amount > 0),
    unapplied_cash_application_status text NOT NULL CHECK (
        unapplied_cash_application_status IN ('applied')
    ),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, unapplied_cash_id),
    UNIQUE (tenant_account_id, unapplied_cash_application_id),
    FOREIGN KEY (tenant_account_id, unapplied_cash_id)
        REFERENCES billing_core.unapplied_cash (tenant_account_id, unapplied_cash_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    FOREIGN KEY (tenant_account_id, payment_receipt_id)
        REFERENCES billing_core.payment_receipt (tenant_account_id, payment_receipt_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
