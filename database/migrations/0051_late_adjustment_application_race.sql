BEGIN;

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_application()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    adjustment billing_core.late_adjustment%ROWTYPE;
    target_period billing_core.billing_period%ROWTYPE;
    target_status text;
BEGIN
    -- Let ON CONFLICT classify an already-stored replay before lifecycle checks.
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

    -- A concurrent first insert can commit while this trigger waits on the
    -- source row; classify that retry before checking the target lifecycle.
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

    IF NEW.applied_at > clock_timestamp() THEN
        RAISE EXCEPTION 'late adjustment application applied_at must not be in the future';
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

COMMIT;
