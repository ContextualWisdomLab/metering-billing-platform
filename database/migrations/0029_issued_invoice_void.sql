BEGIN;

ALTER TABLE billing_core.collection_case
    DROP CONSTRAINT collection_case_collection_case_status_check,
    ADD CONSTRAINT collection_case_collection_case_status_check
        CHECK (collection_case_status IN ('open', 'dunning', 'settled', 'voided'));

ALTER TABLE billing_core.collection_case
    DROP CONSTRAINT collection_case_settled_outstanding_check,
    ADD CONSTRAINT collection_case_closed_outstanding_check
        CHECK (
            (
                collection_case_status IN ('settled', 'voided')
                AND outstanding_amount = 0
            )
            OR collection_case_status IN ('open', 'dunning')
        );

CREATE TABLE billing_core.issued_invoice_void (
    issued_invoice_void_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    issued_invoice_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    collection_case_id uuid,
    issued_invoice_void_contract_version integer NOT NULL CHECK (
        issued_invoice_void_contract_version >= 1
    ),
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    voided_amount numeric(38, 12) NOT NULL CHECK (voided_amount > 0),
    remaining_outstanding_amount numeric(38, 12) NOT NULL CHECK (
        remaining_outstanding_amount = 0
    ),
    issued_invoice_void_status text NOT NULL CHECK (
        issued_invoice_void_status IN ('recorded')
    ),
    voided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, issued_invoice_id),
    UNIQUE (tenant_account_id, issued_invoice_void_id),
    FOREIGN KEY (tenant_account_id, issued_invoice_id)
        REFERENCES billing_core.issued_invoice (tenant_account_id, issued_invoice_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
