BEGIN;

CREATE TABLE billing_core.collection_write_off (
    collection_write_off_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    collection_case_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    issued_invoice_id uuid,
    collection_write_off_contract_version integer NOT NULL CHECK (
        collection_write_off_contract_version >= 1
    ),
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    write_off_amount numeric(38, 12) NOT NULL CHECK (write_off_amount > 0),
    remaining_outstanding_amount numeric(38, 12) NOT NULL CHECK (
        remaining_outstanding_amount = 0
    ),
    collection_write_off_status text NOT NULL CHECK (
        collection_write_off_status IN ('recorded')
    ),
    written_off_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, collection_case_id),
    UNIQUE (tenant_account_id, collection_write_off_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, issued_invoice_id)
        REFERENCES billing_core.issued_invoice (tenant_account_id, issued_invoice_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
