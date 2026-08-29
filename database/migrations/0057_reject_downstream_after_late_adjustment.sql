BEGIN;

CREATE OR REPLACE FUNCTION billing_core.reject_downstream_after_late_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.invoice_draft_id IS NULL THEN
        RETURN NEW;
    END IF;

    PERFORM 1
    FROM billing_core.invoice_draft
    WHERE tenant_account_id = NEW.tenant_account_id
      AND invoice_draft_id = NEW.invoice_draft_id
    FOR UPDATE;

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

CREATE TRIGGER collection_case_reject_late_adjustment
BEFORE INSERT ON billing_core.collection_case
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_downstream_after_late_adjustment();

CREATE TRIGGER tax_assessment_reject_late_adjustment
BEFORE INSERT ON billing_core.tax_assessment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_downstream_after_late_adjustment();

CREATE TRIGGER credit_adjustment_reject_late_adjustment
BEFORE INSERT ON billing_core.credit_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_downstream_after_late_adjustment();

CREATE TRIGGER journal_proposal_reject_late_adjustment
BEFORE INSERT ON billing_core.journal_proposal
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_downstream_after_late_adjustment();

COMMIT;
