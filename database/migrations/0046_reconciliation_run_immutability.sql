BEGIN;

CREATE TRIGGER reconciliation_run_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_run
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_run_line_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_run_line
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
