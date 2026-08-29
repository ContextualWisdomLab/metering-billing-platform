BEGIN;

CREATE OR REPLACE FUNCTION billing_core.validate_fx_conversion_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    referenced_rate billing_core.fx_rate%ROWTYPE;
BEGIN
    SELECT *
    INTO referenced_rate
    FROM billing_core.fx_rate
    WHERE fx_rate_id = NEW.fx_rate_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'FX conversion references a missing rate';
    END IF;

    IF NEW.source_currency IS DISTINCT FROM referenced_rate.base_currency
       OR NEW.quote_currency IS DISTINCT FROM referenced_rate.quote_currency
       OR NEW.fx_rate_value IS DISTINCT FROM referenced_rate.fx_rate_value
       OR NEW.rate_precision IS DISTINCT FROM referenced_rate.rate_precision
    THEN
        RAISE EXCEPTION 'FX conversion snapshot does not match its referenced rate';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER fx_conversion_rate_snapshot_validate
BEFORE INSERT ON billing_core.fx_conversion
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_fx_conversion_snapshot();

COMMIT;
