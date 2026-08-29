BEGIN;

CREATE TABLE billing_core.reconciliation_resolution (
    resolution_id uuid PRIMARY KEY,
    reconciliation_line_id uuid NOT NULL,
    exception_code text NOT NULL CHECK (exception_code IN (
        'currency_mismatch',
        'price_mismatch',
        'provider_fee_mismatch',
        'settlement_mismatch'
    )),
    resolution_status text NOT NULL CHECK (resolution_status IN ('resolved', 'waived')),
    owner_reference text NOT NULL,
    resolution_reason text NOT NULL,
    evidence_reference text NOT NULL,
    maker_reference text NOT NULL,
    checker_reference text NOT NULL,
    resolved_at timestamptz NOT NULL,
    reconciliation_resolution_contract_version integer NOT NULL CHECK (
        reconciliation_resolution_contract_version >= 1
    ),
    FOREIGN KEY (reconciliation_line_id, exception_code)
        REFERENCES billing_core.reconciliation_exception (reconciliation_line_id, exception_code),
    CHECK (owner_reference <> ''),
    CHECK (resolution_reason <> ''),
    CHECK (evidence_reference <> ''),
    CHECK (maker_reference <> ''),
    CHECK (checker_reference <> ''),
    CHECK (maker_reference <> checker_reference)
);

COMMIT;
