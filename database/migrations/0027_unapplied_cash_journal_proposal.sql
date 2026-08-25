BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN unapplied_cash_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_unapplied_cash_fk
        FOREIGN KEY (tenant_account_id, unapplied_cash_id)
            REFERENCES billing_core.unapplied_cash (tenant_account_id, unapplied_cash_id);

CREATE UNIQUE INDEX journal_proposal_unapplied_cash_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        unapplied_cash_id
    )
    WHERE unapplied_cash_id IS NOT NULL;

COMMIT;
