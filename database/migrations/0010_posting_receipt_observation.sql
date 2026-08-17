BEGIN;

CREATE TABLE billing_core.posting_receipt_observation (
    posting_receipt_observation_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    receipt_id uuid NOT NULL,
    receipt_contract_version integer NOT NULL CHECK (receipt_contract_version >= 1),
    idempotency_key text NOT NULL,
    source_proposal_id uuid NOT NULL,
    source_payload_hash text NOT NULL,
    legal_entity_reference text NOT NULL,
    accounting_book_reference text NOT NULL,
    accounting_policy_version text NOT NULL,
    posting_rule_version text NOT NULL,
    posting_status_code text NOT NULL CHECK (posting_status_code IN ('posted', 'held', 'rejected', 'reversed')),
    recorded_at text NOT NULL,
    fiscal_period_reference text,
    journal_reference text,
    reversal_of_journal_reference text,
    hold_reason_code text,
    rejection_reason_code text,
    posted_at text,
    line_count integer CHECK (line_count IS NULL OR line_count >= 0),
    transaction_currency text,
    functional_currency text,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, idempotency_key),
    UNIQUE (tenant_account_id, receipt_id),
    UNIQUE (tenant_account_id, posting_receipt_observation_id),
    FOREIGN KEY (tenant_account_id)
        REFERENCES billing_core.tenant_account (tenant_account_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
