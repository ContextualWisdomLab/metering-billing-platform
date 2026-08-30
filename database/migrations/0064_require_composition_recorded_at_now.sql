BEGIN;

CREATE OR REPLACE FUNCTION billing_core.require_composition_recorded_at_now()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Preserve immutable replay semantics while rejecting future first writes.
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_invoice_adjustment AS existing
        WHERE existing.late_adjustment_invoice_adjustment_id = NEW.late_adjustment_invoice_adjustment_id
           OR (
               existing.tenant_account_id = NEW.tenant_account_id
               AND existing.late_adjustment_rating_id = NEW.late_adjustment_rating_id
           )
    ) THEN
        RETURN NEW;
    END IF;

    IF NEW.recorded_at > clock_timestamp() THEN
        RAISE EXCEPTION 'late adjustment invoice adjustment recorded_at must not be in the future';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_invoice_adjustment_recorded_at_validate
BEFORE INSERT ON billing_core.late_adjustment_invoice_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.require_composition_recorded_at_now();

COMMIT;
