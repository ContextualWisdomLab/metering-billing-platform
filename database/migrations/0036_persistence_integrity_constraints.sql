BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE billing_core.accounting_export_record
    ADD CONSTRAINT accounting_export_record_tenant_proposal_reference_key
    UNIQUE (tenant_account_id, proposal_reference);

ALTER TABLE billing_core.credential_assignment
    ADD CONSTRAINT credential_assignment_no_overlap
    EXCLUDE USING gist (
        credential_record_id WITH =,
        tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)') WITH &&
    );

COMMIT;
