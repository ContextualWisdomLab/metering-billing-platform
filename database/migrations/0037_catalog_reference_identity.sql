BEGIN;

ALTER TABLE billing_core.tenant_account
    ADD COLUMN tenant_reference text;

UPDATE billing_core.tenant_account
SET tenant_reference = 'urn:cwl:' || tenant_account_code
WHERE tenant_reference IS NULL;

ALTER TABLE billing_core.tenant_account
    ALTER COLUMN tenant_reference SET NOT NULL;

ALTER TABLE billing_core.tenant_account
    ADD CONSTRAINT tenant_account_tenant_reference_key
    UNIQUE (tenant_reference);

ALTER TABLE billing_core.credential_record
    ADD COLUMN credential_reference text;

UPDATE billing_core.credential_record AS credential
SET credential_reference =
    'urn:cwl:' || tenant.tenant_account_code
    || ':credential_record:' || credential.credential_record_id::text
FROM billing_core.tenant_account AS tenant
WHERE tenant.tenant_account_id = credential.tenant_account_id
  AND credential.credential_reference IS NULL;

ALTER TABLE billing_core.credential_record
    ALTER COLUMN credential_reference SET NOT NULL;

ALTER TABLE billing_core.credential_record
    ADD CONSTRAINT credential_record_tenant_reference_key
    UNIQUE (tenant_account_id, credential_reference);

COMMIT;
