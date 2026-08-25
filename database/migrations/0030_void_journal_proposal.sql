BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN issued_invoice_void_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_issued_invoice_void_fk
        FOREIGN KEY (tenant_account_id, issued_invoice_void_id)
            REFERENCES billing_core.issued_invoice_void (tenant_account_id, issued_invoice_void_id);

CREATE UNIQUE INDEX journal_proposal_issued_invoice_void_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        issued_invoice_void_id
    )
    WHERE issued_invoice_void_id IS NOT NULL;

COMMIT;
