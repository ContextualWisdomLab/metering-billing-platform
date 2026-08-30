BEGIN;

ALTER TABLE billing_core.late_adjustment_invoice_adjustment
    ADD CONSTRAINT late_adjustment_invoice_adjustment_numeric_38_12_check
    CHECK (adjustment_amount = adjustment_amount::numeric(38, 12));

CREATE OR REPLACE FUNCTION billing_core.reject_downstream_after_late_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    issued billing_core.issued_invoice%ROWTYPE;
BEGIN
    IF NEW.invoice_draft_id IS NULL THEN
        RETURN NEW;
    END IF;

    PERFORM 1
    FROM billing_core.invoice_draft
    WHERE tenant_account_id = NEW.tenant_account_id
      AND invoice_draft_id = NEW.invoice_draft_id
    FOR UPDATE;

    IF TG_TABLE_NAME = 'collection_case' THEN
        SELECT * INTO issued
        FROM billing_core.issued_invoice
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id;
        IF FOUND THEN
            IF NEW.currency_code <> issued.currency_code
               OR NEW.outstanding_amount <> issued.tax_inclusive_amount THEN
                RAISE EXCEPTION 'collection case does not match issued invoice';
            END IF;
            RETURN NEW;
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_invoice_adjustment
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) THEN
        RAISE EXCEPTION 'invoice draft has late adjustment invoice adjustment';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_issued_invoice_line()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    composition billing_core.late_adjustment_invoice_adjustment%ROWTYPE;
    issued_draft_id uuid;
BEGIN
    IF NEW.line_type <> 'late_adjustment' THEN
        RETURN NEW;
    END IF;

    SELECT * INTO composition
    FROM billing_core.late_adjustment_invoice_adjustment
    WHERE tenant_account_id = NEW.tenant_account_id
      AND late_adjustment_invoice_adjustment_id = NEW.late_adjustment_invoice_adjustment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment issued line composition is missing';
    END IF;

    SELECT invoice_draft_id INTO issued_draft_id
    FROM billing_core.issued_invoice
    WHERE tenant_account_id = NEW.tenant_account_id
      AND issued_invoice_id = NEW.issued_invoice_id;
    IF NOT FOUND OR issued_draft_id <> composition.invoice_draft_id THEN
        RAISE EXCEPTION 'late adjustment issued line draft does not match composition';
    END IF;
    IF NEW.billing_account_reference <> composition.billing_account_reference
       OR NEW.line_total_amount <> composition.adjustment_amount
       OR NEW.unit_price_amount <> abs(composition.adjustment_amount)
       OR NEW.rated_quantity <> 1 THEN
        RAISE EXCEPTION 'late adjustment issued line does not match composition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER issued_invoice_line_late_adjustment_evidence_validate
BEFORE INSERT ON billing_core.issued_invoice_line
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_late_adjustment_issued_invoice_line();

COMMIT;
