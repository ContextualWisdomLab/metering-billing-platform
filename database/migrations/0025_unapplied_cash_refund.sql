BEGIN;

CREATE TABLE billing_core.unapplied_cash_refund (
    unapplied_cash_refund_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    unapplied_cash_id uuid NOT NULL,
    payment_receipt_id uuid NOT NULL,
    payment_intent_id uuid NOT NULL,
    collection_case_id uuid NOT NULL,
    unapplied_cash_refund_contract_version integer NOT NULL CHECK (
        unapplied_cash_refund_contract_version >= 1
    ),
    source_payload_hash text NOT NULL,
    currency_code text NOT NULL,
    refund_amount numeric(38, 12) NOT NULL CHECK (refund_amount > 0),
    unapplied_amount numeric(38, 12) NOT NULL CHECK (unapplied_amount > 0),
    unapplied_cash_refund_status text NOT NULL CHECK (
        unapplied_cash_refund_status IN ('recorded')
    ),
    refunded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, unapplied_cash_id),
    UNIQUE (tenant_account_id, unapplied_cash_refund_id),
    FOREIGN KEY (tenant_account_id, unapplied_cash_id)
        REFERENCES billing_core.unapplied_cash (tenant_account_id, unapplied_cash_id),
    FOREIGN KEY (tenant_account_id, payment_receipt_id)
        REFERENCES billing_core.payment_receipt (tenant_account_id, payment_receipt_id),
    FOREIGN KEY (tenant_account_id, payment_intent_id)
        REFERENCES billing_core.payment_intent (tenant_account_id, payment_intent_id),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id),
    CHECK (refund_amount = unapplied_amount),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
