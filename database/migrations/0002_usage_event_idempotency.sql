BEGIN;

ALTER TABLE billing_core.usage_event
    ADD COLUMN event_contract_version integer,
    ADD COLUMN cost_center_reference text,
    ADD COLUMN project_reference text;

ALTER TABLE billing_core.usage_event
    ALTER COLUMN event_contract_version SET NOT NULL;

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_contract_version_positive
        CHECK (event_contract_version > 0);

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_payload_hash_format
        CHECK (event_payload_hash ~ '^sha256:[0-9a-f]{64}$');

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_payload_hash_unique
        UNIQUE (tenant_account_id, event_payload_hash, event_contract_version);

CREATE INDEX usage_event_tenant_occurred_at
    ON billing_core.usage_event (tenant_account_id, occurred_at);

CREATE TABLE billing_core.usage_ingestion_receipt (
    usage_ingestion_receipt_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid REFERENCES billing_core.tenant_account (tenant_account_id),
    usage_event_id uuid REFERENCES billing_core.usage_event (usage_event_id),
    source_event_key text NOT NULL,
    event_contract_version integer,
    source_payload_hash text,
    ingestion_outcome_code text NOT NULL CHECK (
        ingestion_outcome_code IN ('accepted', 'duplicate_replay', 'rejected')
    ),
    rejection_reason_code text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (event_contract_version IS NULL OR event_contract_version > 0),
    CHECK (
        source_payload_hash IS NULL
        OR source_payload_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

COMMIT;
