BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN payment_receipt_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_payment_receipt_fk
        FOREIGN KEY (tenant_account_id, payment_receipt_id)
            REFERENCES billing_core.payment_receipt (tenant_account_id, payment_receipt_id);

CREATE UNIQUE INDEX journal_proposal_receipt_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        payment_receipt_id,
        source_payload_hash,
        proposal_contract_version
    )
    WHERE payment_receipt_id IS NOT NULL;

COMMIT;
