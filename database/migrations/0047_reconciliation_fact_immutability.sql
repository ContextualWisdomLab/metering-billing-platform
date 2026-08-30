BEGIN;

CREATE TRIGGER billing_period_immutable
BEFORE UPDATE OR DELETE ON billing_core.billing_period
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER fx_rate_immutable
BEFORE UPDATE OR DELETE ON billing_core.fx_rate
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER fx_conversion_immutable
BEFORE UPDATE OR DELETE ON billing_core.fx_conversion
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_exception_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_exception
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
