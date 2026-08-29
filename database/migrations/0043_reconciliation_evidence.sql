BEGIN;

CREATE TABLE billing_core.reconciliation_evidence (
    evidence_id uuid PRIMARY KEY,
    reconciliation_line_id uuid NOT NULL,
    exception_code text NOT NULL CHECK (exception_code IN (
        'quantity_mismatch',
        'price_mismatch',
        'tax_mismatch',
        'currency_mismatch',
        'payment_missing',
        'duplicate_charge',
        'refund_mismatch',
        'dispute_mismatch',
        'settlement_mismatch',
        'provider_fee_mismatch',
        'cash_timing_difference',
        'unmapped_provider_object'
    )),
    evidence_kind text NOT NULL,
    evidence_reference text NOT NULL,
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    captured_by text NOT NULL,
    captured_at timestamptz NOT NULL,
    reconciliation_evidence_contract_version integer NOT NULL CHECK (
        reconciliation_evidence_contract_version >= 1
    ),
    FOREIGN KEY (reconciliation_line_id, exception_code)
        REFERENCES billing_core.reconciliation_exception (reconciliation_line_id, exception_code),
    CHECK (evidence_kind <> ''),
    CHECK (evidence_reference <> ''),
    CHECK (captured_by <> '')
);

COMMIT;
