BEGIN;

CREATE TABLE billing_core.credit_adjustment (
    credit_adjustment_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    invoice_draft_id uuid NOT NULL,
    credit_adjustment_contract_version integer NOT NULL CHECK (credit_adjustment_contract_version >= 1),
    credit_reason_code text NOT NULL CHECK (credit_reason_code IN ('rating_correction', 'goodwill', 'billing_error')),
    currency_code text NOT NULL,
    credit_amount numeric(38, 12) NOT NULL CHECK (credit_amount > 0),
    source_payload_hash text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version),
    UNIQUE (tenant_account_id, credit_adjustment_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN credit_adjustment_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_credit_adjustment_fk
        FOREIGN KEY (tenant_account_id, credit_adjustment_id)
            REFERENCES billing_core.credit_adjustment (tenant_account_id, credit_adjustment_id);

CREATE UNIQUE INDEX journal_proposal_credit_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        credit_adjustment_id,
        source_payload_hash,
        proposal_contract_version
    )
    WHERE credit_adjustment_id IS NOT NULL;

COMMIT;
