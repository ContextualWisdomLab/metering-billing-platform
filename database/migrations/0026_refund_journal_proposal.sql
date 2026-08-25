BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN unapplied_cash_refund_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_unapplied_cash_refund_fk
        FOREIGN KEY (tenant_account_id, unapplied_cash_refund_id)
            REFERENCES billing_core.unapplied_cash_refund (tenant_account_id, unapplied_cash_refund_id);

CREATE UNIQUE INDEX journal_proposal_refund_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        unapplied_cash_refund_id
    )
    WHERE unapplied_cash_refund_id IS NOT NULL;

COMMIT;
