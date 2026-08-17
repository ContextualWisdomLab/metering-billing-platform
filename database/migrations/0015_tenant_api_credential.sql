BEGIN;

CREATE TABLE billing_core.tenant_api_credential (
    tenant_api_credential_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    tenant_api_credential_contract_version integer NOT NULL CHECK (tenant_api_credential_contract_version >= 1),
    credential_label text NOT NULL CHECK (credential_label ~ '^[a-z][a-z0-9]*_[a-z0-9_]+$'),
    credential_prefix text NOT NULL,
    credential_secret_hash text NOT NULL CHECK (credential_secret_hash ~ '^hmac-sha256:[0-9a-f]{64}$'),
    credential_status text NOT NULL CHECK (credential_status IN ('active', 'revoked')),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz,
    UNIQUE (tenant_account_id, tenant_api_credential_id),
    UNIQUE (credential_secret_hash),
    CHECK (revoked_at IS NULL OR revoked_at >= issued_at)
);

COMMIT;
