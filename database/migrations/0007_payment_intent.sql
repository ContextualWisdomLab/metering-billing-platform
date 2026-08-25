BEGIN;

CREATE TABLE billing_core.payment_intent (
    payment_intent_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    collection_case_id uuid NOT NULL,
    payment_intent_contract_version integer NOT NULL CHECK (payment_intent_contract_version >= 1),
    currency_code text NOT NULL,
    payment_intent_status text NOT NULL CHECK (payment_intent_status IN ('projected', 'cancelled', 'rejected')),
    payment_amount numeric(38, 12) NOT NULL CHECK (payment_amount > 0),
    source_payload_hash text NOT NULL,
    projected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version),
    UNIQUE (tenant_account_id, payment_intent_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
