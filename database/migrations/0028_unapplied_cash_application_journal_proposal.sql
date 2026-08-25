BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN unapplied_cash_application_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_unapplied_cash_application_fk
        FOREIGN KEY (tenant_account_id, unapplied_cash_application_id)
            REFERENCES billing_core.unapplied_cash_application (
                tenant_account_id,
                unapplied_cash_application_id
            );

CREATE UNIQUE INDEX journal_proposal_unapplied_cash_application_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        unapplied_cash_application_id
    )
    WHERE unapplied_cash_application_id IS NOT NULL;

COMMIT;
