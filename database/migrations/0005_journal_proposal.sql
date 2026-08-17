BEGIN;

CREATE TABLE billing_core.journal_proposal (
    journal_proposal_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    invoice_draft_id uuid NOT NULL,
    proposal_contract_version integer NOT NULL CHECK (proposal_contract_version >= 1),
    idempotency_key text NOT NULL,
    legal_entity_reference text NOT NULL,
    intended_book_role_code text NOT NULL,
    transaction_currency text NOT NULL,
    transaction_date date NOT NULL,
    accounting_date date NOT NULL,
    source_payload_hash text NOT NULL,
    proposed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    proposal_status text NOT NULL CHECK (proposal_status IN ('draft', 'validated', 'exported', 'rejected')),
    source_event_reference text NOT NULL,
    UNIQUE (tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version),
    UNIQUE (tenant_account_id, journal_proposal_id),
    UNIQUE (tenant_account_id, idempotency_key),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (legal_entity_reference ~ '^urn:cwl:'),
    CHECK (source_event_reference ~ '^urn:cwl:')
);

CREATE TABLE billing_core.journal_proposal_line (
    journal_proposal_line_id uuid PRIMARY KEY DEFAULT uuidv7(),
    journal_proposal_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number > 0),
    account_role_code text NOT NULL,
    debit_amount numeric(38, 12) NOT NULL CHECK (debit_amount >= 0),
    credit_amount numeric(38, 12) NOT NULL CHECK (credit_amount >= 0),
    UNIQUE (journal_proposal_id, line_number),
    FOREIGN KEY (tenant_account_id, journal_proposal_id)
        REFERENCES billing_core.journal_proposal (tenant_account_id, journal_proposal_id),
    CHECK (
        (debit_amount > 0 AND credit_amount = 0)
        OR (credit_amount > 0 AND debit_amount = 0)
    )
);

COMMIT;
