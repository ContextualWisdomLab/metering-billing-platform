BEGIN;

ALTER TABLE billing_core.journal_proposal
    ADD COLUMN collection_write_off_id uuid;

ALTER TABLE billing_core.journal_proposal
    ADD CONSTRAINT journal_proposal_collection_write_off_fk
        FOREIGN KEY (tenant_account_id, collection_write_off_id)
            REFERENCES billing_core.collection_write_off (tenant_account_id, collection_write_off_id);

CREATE UNIQUE INDEX journal_proposal_write_off_identity
    ON billing_core.journal_proposal (
        tenant_account_id,
        collection_write_off_id
    )
    WHERE collection_write_off_id IS NOT NULL;

COMMIT;
