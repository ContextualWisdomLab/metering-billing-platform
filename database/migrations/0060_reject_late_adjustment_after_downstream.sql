BEGIN;

CREATE OR REPLACE FUNCTION billing_core.reject_late_adjustment_after_downstream()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
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

    PERFORM 1
    FROM billing_core.invoice_draft
    WHERE tenant_account_id = NEW.tenant_account_id
      AND invoice_draft_id = NEW.invoice_draft_id
    FOR UPDATE;

    IF EXISTS (
        SELECT 1
        FROM billing_core.collection_case
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) OR EXISTS (
        SELECT 1
        FROM billing_core.tax_assessment
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) OR EXISTS (
        SELECT 1
        FROM billing_core.credit_adjustment
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) OR EXISTS (
        SELECT 1
        FROM billing_core.journal_proposal
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) THEN
        RAISE EXCEPTION 'invoice draft has downstream records';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_invoice_adjustment_reject_downstream
BEFORE INSERT ON billing_core.late_adjustment_invoice_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_late_adjustment_after_downstream();

COMMIT;
