BEGIN;

DROP TRIGGER late_adjustment_invoice_adjustment_immutable
ON billing_core.late_adjustment_invoice_adjustment;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_invoice_adjustment
        WHERE late_adjustment_invoice_adjustment_contract_version = 1
          AND (
              billing_account_id IS NULL
              OR billing_account_reference IS NULL
          )
    ) THEN
        RAISE EXCEPTION
            'legacy late adjustment invoice adjustment requires billing account migration';
    END IF;

    UPDATE billing_core.late_adjustment_invoice_adjustment
    SET late_adjustment_invoice_adjustment_contract_version = 2
    WHERE late_adjustment_invoice_adjustment_contract_version = 1;
END;
$$;

ALTER TABLE billing_core.late_adjustment_invoice_adjustment
    ADD CONSTRAINT late_adjustment_invoice_adjustment_contract_version_v2_check
    CHECK (late_adjustment_invoice_adjustment_contract_version = 2);

CREATE TRIGGER late_adjustment_invoice_adjustment_immutable
BEFORE UPDATE OR DELETE ON billing_core.late_adjustment_invoice_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
