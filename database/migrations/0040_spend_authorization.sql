BEGIN;

CREATE TABLE billing_core.spend_authorization (
    spend_authorization_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    billing_account_id uuid NOT NULL,
    spend_budget_id uuid NOT NULL,
    spend_authorization_contract_version integer NOT NULL CHECK (spend_authorization_contract_version >= 1),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
    actor_reference text NOT NULL CHECK (char_length(actor_reference) BETWEEN 1 AND 200),
    purpose_code text NOT NULL CHECK (char_length(purpose_code) BETWEEN 1 AND 100),
    policy_version text NOT NULL CHECK (char_length(policy_version) BETWEEN 1 AND 100),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    requested_amount numeric(38, 12) NOT NULL CHECK (requested_amount > 0),
    reserved_amount numeric(38, 12) NOT NULL CHECK (reserved_amount >= 0),
    committed_amount numeric(38, 12) NOT NULL DEFAULT 0 CHECK (committed_amount >= 0),
    released_amount numeric(38, 12) NOT NULL DEFAULT 0 CHECK (released_amount >= 0),
    valid_from timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    authorization_status text NOT NULL CHECK (
        authorization_status IN (
            'requested', 'reserved', 'partially_committed', 'committed',
            'released', 'expired', 'denied'
        )
    ),
    rejection_reason_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_account_id, spend_authorization_id),
    UNIQUE (tenant_account_id, idempotency_key),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id),
    FOREIGN KEY (tenant_account_id, spend_budget_id)
        REFERENCES billing_core.spend_budget (tenant_account_id, spend_budget_id),
    CHECK (reserved_amount = requested_amount OR authorization_status = 'denied'),
    CHECK (committed_amount + released_amount <= requested_amount),
    CHECK (valid_until > valid_from)
);

CREATE TABLE billing_core.spend_reservation (
    spend_reservation_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    spend_authorization_id uuid NOT NULL,
    reserved_amount numeric(38, 12) NOT NULL CHECK (reserved_amount > 0),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
    reserved_at timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    UNIQUE (tenant_account_id, spend_reservation_id),
    UNIQUE (tenant_account_id, spend_authorization_id),
    FOREIGN KEY (tenant_account_id, spend_authorization_id)
        REFERENCES billing_core.spend_authorization (tenant_account_id, spend_authorization_id),
    CHECK (valid_until > reserved_at)
);

CREATE TABLE billing_core.spend_commitment (
    spend_commitment_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    spend_authorization_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
    committed_amount numeric(38, 12) NOT NULL CHECK (committed_amount > 0),
    actual_usage_reference text NOT NULL CHECK (char_length(actual_usage_reference) BETWEEN 1 AND 200),
    committed_at timestamptz NOT NULL,
    UNIQUE (tenant_account_id, spend_commitment_id),
    UNIQUE (tenant_account_id, spend_authorization_id, idempotency_key),
    FOREIGN KEY (tenant_account_id, spend_authorization_id)
        REFERENCES billing_core.spend_authorization (tenant_account_id, spend_authorization_id)
);

CREATE TABLE billing_core.spend_release (
    spend_release_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    spend_authorization_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
    released_amount numeric(38, 12) NOT NULL CHECK (released_amount > 0),
    release_reason_code text NOT NULL CHECK (char_length(release_reason_code) BETWEEN 1 AND 100),
    released_at timestamptz NOT NULL,
    UNIQUE (tenant_account_id, spend_release_id),
    UNIQUE (tenant_account_id, spend_authorization_id, idempotency_key),
    FOREIGN KEY (tenant_account_id, spend_authorization_id)
        REFERENCES billing_core.spend_authorization (tenant_account_id, spend_authorization_id)
);

CREATE TABLE billing_core.spend_authorization_transition (
    spend_authorization_transition_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    spend_authorization_id uuid NOT NULL,
    from_status text NOT NULL,
    to_status text NOT NULL,
    causation_key text NOT NULL CHECK (char_length(causation_key) BETWEEN 1 AND 200),
    actor_reference text NOT NULL CHECK (char_length(actor_reference) BETWEEN 1 AND 200),
    occurred_at timestamptz NOT NULL,
    UNIQUE (tenant_account_id, spend_authorization_transition_id),
    FOREIGN KEY (tenant_account_id, spend_authorization_id)
        REFERENCES billing_core.spend_authorization (tenant_account_id, spend_authorization_id),
    CHECK (from_status <> to_status)
);

CREATE INDEX spend_authorization_budget_index
    ON billing_core.spend_authorization (tenant_account_id, spend_budget_id, authorization_status);

CREATE INDEX spend_authorization_transition_index
    ON billing_core.spend_authorization_transition (tenant_account_id, spend_authorization_id, occurred_at);

COMMIT;
