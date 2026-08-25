BEGIN;

ALTER TABLE billing_core.collection_case
    DROP CONSTRAINT collection_case_collection_case_status_check,
    ADD CONSTRAINT collection_case_collection_case_status_check
        CHECK (collection_case_status IN ('open', 'dunning', 'settled'));

ALTER TABLE billing_core.collection_case
    DROP CONSTRAINT collection_case_outstanding_amount_check,
    ADD CONSTRAINT collection_case_outstanding_amount_check
        CHECK (outstanding_amount >= 0);

ALTER TABLE billing_core.collection_case
    ADD CONSTRAINT collection_case_settled_outstanding_check
        CHECK (
            (collection_case_status = 'settled' AND outstanding_amount = 0)
            OR (collection_case_status IN ('open', 'dunning') AND outstanding_amount > 0)
        );

CREATE TABLE billing_core.payment_receipt (
    payment_receipt_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    payment_intent_id uuid NOT NULL,
    collection_case_id uuid NOT NULL,
    settlement_contract_version integer NOT NULL CHECK (settlement_contract_version >= 1),
    currency_code text NOT NULL,
    payment_receipt_status text NOT NULL CHECK (payment_receipt_status IN ('applied')),
    received_amount numeric(38, 12) NOT NULL CHECK (received_amount > 0),
    source_payload_hash text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version),
    UNIQUE (tenant_account_id, payment_receipt_id),
    FOREIGN KEY (tenant_account_id, payment_intent_id)
        REFERENCES billing_core.payment_intent (tenant_account_id, payment_intent_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
