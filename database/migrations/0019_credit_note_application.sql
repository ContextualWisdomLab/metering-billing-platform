BEGIN;

CREATE TABLE billing_core.credit_note_application (
    credit_note_application_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    issued_credit_note_id uuid NOT NULL,
    collection_case_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    issued_invoice_id uuid,
    credit_note_application_contract_version integer NOT NULL CHECK (
        credit_note_application_contract_version >= 1
    ),
    issued_credit_note_contract_version integer NOT NULL CHECK (
        issued_credit_note_contract_version >= 1
    ),
    source_payload_hash text NOT NULL,
    issued_credit_note_source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    applied_amount numeric(38, 12) NOT NULL CHECK (applied_amount > 0),
    credit_note_application_status text NOT NULL CHECK (
        credit_note_application_status IN ('applied')
    ),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, issued_credit_note_id),
    UNIQUE (tenant_account_id, credit_note_application_id),
    FOREIGN KEY (tenant_account_id, issued_credit_note_id)
        REFERENCES billing_core.issued_credit_note (tenant_account_id, issued_credit_note_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, issued_invoice_id)
        REFERENCES billing_core.issued_invoice (tenant_account_id, issued_invoice_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (issued_credit_note_source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
