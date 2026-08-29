BEGIN;

CREATE TABLE billing_core.late_adjustment (
    late_adjustment_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    source_period_id uuid NOT NULL,
    target_period_id uuid NOT NULL,
    adjustment_kind text NOT NULL CHECK (adjustment_kind IN (
        'late_usage',
        'correction',
        'reversal'
    )),
    adjustment_amount numeric NOT NULL CHECK (adjustment_amount <> 0),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    source_reference text NOT NULL CHECK (source_reference <> ''),
    source_payload_hash text NOT NULL CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    late_adjustment_contract_version integer NOT NULL CHECK (
        late_adjustment_contract_version >= 1
    ),
    UNIQUE (
        tenant_account_id,
        source_period_id,
        target_period_id,
        adjustment_kind,
        source_payload_hash,
        late_adjustment_contract_version
    ),
    FOREIGN KEY (tenant_account_id, source_period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id),
    FOREIGN KEY (tenant_account_id, target_period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id),
    CHECK (source_period_id <> target_period_id)
);

CREATE INDEX late_adjustment_target_period_recorded_idx
    ON billing_core.late_adjustment (tenant_account_id, target_period_id, recorded_at);

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_periods()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_period billing_core.billing_period%ROWTYPE;
    target_period billing_core.billing_period%ROWTYPE;
    source_status text;
    target_status text;
BEGIN
    SELECT *
    INTO source_period
    FROM billing_core.billing_period
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.source_period_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment source period is missing';
    END IF;

    SELECT *
    INTO target_period
    FROM billing_core.billing_period
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.target_period_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment target period is missing';
    END IF;

    SELECT COALESCE((
        SELECT transition.to_status
        FROM billing_core.billing_period_transition AS transition
        WHERE transition.tenant_account_id = source_period.tenant_account_id
          AND transition.period_id = source_period.period_id
        ORDER BY transition.transition_number DESC
        LIMIT 1
    ), 'open')
    INTO source_status;

    SELECT COALESCE((
        SELECT transition.to_status
        FROM billing_core.billing_period_transition AS transition
        WHERE transition.tenant_account_id = target_period.tenant_account_id
          AND transition.period_id = target_period.period_id
        ORDER BY transition.transition_number DESC
        LIMIT 1
    ), 'open')
    INTO target_status;

    IF source_status = 'open' THEN
        RAISE EXCEPTION 'late adjustment source period must be closed';
    END IF;
    IF target_status <> 'open' THEN
        RAISE EXCEPTION 'late adjustment target period must be open';
    END IF;
    IF target_period.period_start < source_period.period_end THEN
        RAISE EXCEPTION 'late adjustment target period must follow source period';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_period_validate
BEFORE INSERT ON billing_core.late_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_late_adjustment_periods();

CREATE TRIGGER late_adjustment_immutable
BEFORE UPDATE OR DELETE ON billing_core.late_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
