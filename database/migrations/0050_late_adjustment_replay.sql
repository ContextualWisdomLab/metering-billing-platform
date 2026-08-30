BEGIN;

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_periods()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_period billing_core.billing_period%ROWTYPE;
    target_period billing_core.billing_period%ROWTYPE;
    source_transition RECORD;
    source_status text;
    target_status text;
    expected_source_status text := 'open';
    expected_source_transition_number integer := 1;
BEGIN
    -- A duplicate replay must reach ON CONFLICT handling even if the target
    -- period has since closed. New facts still take the lifecycle path below.
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment AS existing
        WHERE existing.late_adjustment_id = NEW.late_adjustment_id
           OR (
               existing.tenant_account_id = NEW.tenant_account_id
               AND (
                   existing.source_reference = NEW.source_reference
                   OR (
                       existing.source_period_id = NEW.source_period_id
                       AND existing.target_period_id = NEW.target_period_id
                       AND existing.adjustment_kind = NEW.adjustment_kind
                       AND existing.source_payload_hash = NEW.source_payload_hash
                       AND existing.late_adjustment_contract_version =
                           NEW.late_adjustment_contract_version
                   )
               )
           )
    ) THEN
        RETURN NEW;
    END IF;

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

    FOR source_transition IN
        SELECT transition_number, from_status, to_status
        FROM billing_core.billing_period_transition
        WHERE tenant_account_id = source_period.tenant_account_id
          AND period_id = source_period.period_id
        ORDER BY transition_number
    LOOP
        IF source_transition.transition_number <> expected_source_transition_number
           OR source_transition.from_status <> expected_source_status THEN
            RAISE EXCEPTION 'late adjustment source period history is malformed';
        END IF;
        expected_source_status := source_transition.to_status;
        expected_source_transition_number := expected_source_transition_number + 1;
    END LOOP;
    source_status := expected_source_status;

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

COMMIT;
