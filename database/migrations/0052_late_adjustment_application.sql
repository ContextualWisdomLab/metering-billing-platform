BEGIN;

ALTER TABLE billing_core.late_adjustment
    ADD CONSTRAINT late_adjustment_id_tenant_unique
    UNIQUE (late_adjustment_id, tenant_account_id);

CREATE TABLE billing_core.late_adjustment_application (
    late_adjustment_application_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    late_adjustment_id uuid NOT NULL,
    target_period_id uuid NOT NULL,
    adjustment_amount numeric NOT NULL CHECK (
        adjustment_amount <> 0
        AND adjustment_amount NOT IN (
            'NaN'::numeric,
            'Infinity'::numeric,
            '-Infinity'::numeric
        )
        AND length(adjustment_amount::text) <= 40
    ),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    applied_by text NOT NULL CHECK (btrim(applied_by) <> ''),
    authorization_reference text NOT NULL CHECK (btrim(authorization_reference) <> ''),
    applied_at timestamptz NOT NULL,
    late_adjustment_application_contract_version integer NOT NULL CHECK (
        late_adjustment_application_contract_version >= 1
    ),
    late_adjustment_application_status text NOT NULL CHECK (
        late_adjustment_application_status = 'applied'
    ),
    UNIQUE (tenant_account_id, late_adjustment_id),
    FOREIGN KEY (late_adjustment_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment (late_adjustment_id, tenant_account_id),
    FOREIGN KEY (tenant_account_id, target_period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id)
);

CREATE INDEX late_adjustment_application_tenant_applied_idx
    ON billing_core.late_adjustment_application
        (tenant_account_id, applied_at, late_adjustment_application_id);

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_application()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    adjustment billing_core.late_adjustment%ROWTYPE;
    target_period billing_core.billing_period%ROWTYPE;
    target_status text;
BEGIN
    -- Let ON CONFLICT classify both generated-id and tenant/source replays,
    -- even if the target period has since advanced or closed.
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_application AS existing
        WHERE existing.late_adjustment_application_id = NEW.late_adjustment_application_id
           OR (
               existing.tenant_account_id = NEW.tenant_account_id
               AND existing.late_adjustment_id = NEW.late_adjustment_id
           )
    ) THEN
        RETURN NEW;
    END IF;

    SELECT *
    INTO adjustment
    FROM billing_core.late_adjustment
    WHERE late_adjustment_id = NEW.late_adjustment_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment application source is missing';
    END IF;
    IF NEW.target_period_id <> adjustment.target_period_id THEN
        RAISE EXCEPTION 'late adjustment application target does not match source';
    END IF;
    IF NEW.adjustment_amount <> adjustment.adjustment_amount THEN
        RAISE EXCEPTION 'late adjustment application amount does not match source';
    END IF;
    IF NEW.currency_code <> adjustment.currency_code THEN
        RAISE EXCEPTION 'late adjustment application currency does not match source';
    END IF;

    SELECT *
    INTO target_period
    FROM billing_core.billing_period
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = adjustment.target_period_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment application target period is missing';
    END IF;

    SELECT COALESCE((
        SELECT transition.to_status
        FROM billing_core.billing_period_transition AS transition
        WHERE transition.tenant_account_id = target_period.tenant_account_id
          AND transition.period_id = target_period.period_id
        ORDER BY transition.transition_number DESC
        LIMIT 1
    ), 'open')
    INTO target_status;
    IF target_status <> 'open' THEN
        RAISE EXCEPTION 'late adjustment application target period must be open';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_application_validate
BEFORE INSERT ON billing_core.late_adjustment_application
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_late_adjustment_application();

CREATE TRIGGER late_adjustment_application_immutable
BEFORE UPDATE OR DELETE ON billing_core.late_adjustment_application
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
