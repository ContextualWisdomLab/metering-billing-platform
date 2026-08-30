BEGIN;

CREATE OR REPLACE FUNCTION billing_core.validate_issued_invoice_composition_completeness()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    tenant_id uuid;
    issued_id uuid;
    draft_id uuid;
    issued_exclusive numeric;
    drafted_total numeric;
    expected_count bigint;
    actual_count bigint;
    expected_adjustment numeric;
    actual_adjustment numeric;
BEGIN
    IF TG_TABLE_NAME = 'issued_invoice' THEN
        tenant_id := NEW.tenant_account_id;
        issued_id := NEW.issued_invoice_id;
        draft_id := NEW.invoice_draft_id;
        issued_exclusive := NEW.tax_exclusive_amount;
    ELSE
        tenant_id := NEW.tenant_account_id;
        issued_id := NEW.issued_invoice_id;
        SELECT invoice_draft_id, tax_exclusive_amount
        INTO draft_id, issued_exclusive
        FROM billing_core.issued_invoice
        WHERE tenant_account_id = NEW.tenant_account_id
          AND issued_invoice_id = NEW.issued_invoice_id;
    END IF;

    PERFORM 1
    FROM billing_core.invoice_draft
    WHERE tenant_account_id = tenant_id
      AND invoice_draft_id = draft_id
    FOR UPDATE;

    SELECT drafted_total_amount
    INTO drafted_total
    FROM billing_core.invoice_draft
    WHERE tenant_account_id = tenant_id
      AND invoice_draft_id = draft_id;

    SELECT count(*), COALESCE(sum(adjustment_amount), 0)
    INTO expected_count, expected_adjustment
    FROM billing_core.late_adjustment_invoice_adjustment
    WHERE tenant_account_id = tenant_id
      AND invoice_draft_id = draft_id;

    SELECT count(*), COALESCE(sum(line_total_amount), 0)
    INTO actual_count, actual_adjustment
    FROM billing_core.issued_invoice_line
    WHERE tenant_account_id = tenant_id
      AND issued_invoice_id = issued_id
      AND line_type = 'late_adjustment';

    IF expected_count <> actual_count THEN
        RAISE EXCEPTION 'issued invoice is missing late adjustment lines';
    END IF;
    IF expected_count > 0
       AND issued_exclusive <> drafted_total + expected_adjustment THEN
        RAISE EXCEPTION 'issued invoice total does not include late adjustments';
    END IF;
    IF expected_count > 0 AND expected_adjustment <> actual_adjustment THEN
        RAISE EXCEPTION 'issued invoice late adjustment lines do not sum to compositions';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER issued_invoice_composition_completeness_validate
AFTER INSERT ON billing_core.issued_invoice
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_issued_invoice_composition_completeness();

CREATE CONSTRAINT TRIGGER issued_invoice_line_composition_completeness_validate
AFTER INSERT ON billing_core.issued_invoice_line
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_issued_invoice_composition_completeness();

COMMIT;
