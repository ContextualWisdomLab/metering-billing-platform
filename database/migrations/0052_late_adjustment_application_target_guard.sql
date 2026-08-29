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
    -- Preserve replay after a target closes; only new applications need the
    -- current target-period lifecycle check.
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
    SELECT *
    INTO target_period
    FROM billing_core.billing_period
    WHERE period_id = NEW.target_period_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment application target is missing';
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
    IF NEW.target_period_id <> adjustment.target_period_id THEN
        RAISE EXCEPTION 'late adjustment application target does not match source';
    END IF;
    IF NEW.adjustment_amount <> adjustment.adjustment_amount THEN
        RAISE EXCEPTION 'late adjustment application amount does not match source';
    END IF;
    IF NEW.currency_code <> adjustment.currency_code THEN
        RAISE EXCEPTION 'late adjustment application currency does not match source';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
