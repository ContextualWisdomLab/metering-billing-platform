BEGIN;

ALTER TABLE billing_core.issued_invoice_line
    ALTER COLUMN line_type DROP DEFAULT;

CREATE TRIGGER issued_invoice_immutable
BEFORE UPDATE OR DELETE ON billing_core.issued_invoice
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER issued_invoice_line_immutable
BEFORE UPDATE OR DELETE ON billing_core.issued_invoice_line
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
