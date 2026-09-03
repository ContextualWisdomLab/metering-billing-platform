BEGIN;

ALTER TABLE billing_core.reconciliation_line
    ADD CONSTRAINT reconciliation_line_tenant_period_identity
    UNIQUE (tenant_account_id, period_id, reconciliation_line_id);

CREATE TABLE billing_core.reconciliation_run (
    run_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    period_id uuid NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    blocking_exception_count integer NOT NULL CHECK (blocking_exception_count >= 0),
    reconciliation_run_contract_version integer NOT NULL CHECK (
        reconciliation_run_contract_version >= 1
    ),
    UNIQUE (tenant_account_id, period_id, run_id),
    FOREIGN KEY (tenant_account_id, period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id),
    CHECK (completed_at >= started_at)
);

CREATE TABLE billing_core.reconciliation_run_line (
    run_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    period_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number > 0),
    reconciliation_line_id uuid NOT NULL,
    PRIMARY KEY (run_id, line_number),
    UNIQUE (run_id, reconciliation_line_id),
    FOREIGN KEY (tenant_account_id, period_id, run_id)
        REFERENCES billing_core.reconciliation_run (tenant_account_id, period_id, run_id),
    FOREIGN KEY (tenant_account_id, period_id, reconciliation_line_id)
        REFERENCES billing_core.reconciliation_line (
            tenant_account_id, period_id, reconciliation_line_id
        )
);

COMMIT;
