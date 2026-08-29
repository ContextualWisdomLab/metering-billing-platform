BEGIN;

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_rating()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    application billing_core.late_adjustment_application%ROWTYPE;
    target_status text;
BEGIN
    -- Preserve replay after a target closes; only new ratings need the
    -- current target-period lifecycle check.
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_rating AS existing
        WHERE existing.late_adjustment_rating_id = NEW.late_adjustment_rating_id
           OR (
               existing.tenant_account_id = NEW.tenant_account_id
               AND (
                   existing.late_adjustment_application_id = NEW.late_adjustment_application_id
                   OR existing.late_adjustment_id = NEW.late_adjustment_id
               )
           )
    ) THEN
        RETURN NEW;
    END IF;

    PERFORM 1
    FROM billing_core.billing_period
    WHERE period_id = NEW.target_period_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment rating target is missing';
    END IF;
    SELECT COALESCE((
        SELECT transition.to_status
        FROM billing_core.billing_period_transition AS transition
        WHERE transition.tenant_account_id = NEW.tenant_account_id
          AND transition.period_id = NEW.target_period_id
        ORDER BY transition.transition_number DESC
        LIMIT 1
    ), 'open')
    INTO target_status;
    IF target_status <> 'open' THEN
        RAISE EXCEPTION 'late adjustment rating target period must be open';
    END IF;

    SELECT *
    INTO application
    FROM billing_core.late_adjustment_application
    WHERE late_adjustment_application_id = NEW.late_adjustment_application_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment rating application is missing';
    END IF;
    IF NEW.late_adjustment_id <> application.late_adjustment_id THEN
        RAISE EXCEPTION 'late adjustment rating source does not match application';
    END IF;
    IF NEW.target_period_id <> application.target_period_id THEN
        RAISE EXCEPTION 'late adjustment rating target does not match application';
    END IF;
    IF NEW.adjustment_amount <> application.adjustment_amount THEN
        RAISE EXCEPTION 'late adjustment rating amount does not match application';
    END IF;
    IF NEW.currency_code <> application.currency_code THEN
        RAISE EXCEPTION 'late adjustment rating currency does not match application';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
