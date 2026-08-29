BEGIN;

CREATE OR REPLACE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable reconciliation facts cannot be updated or deleted';
END;
$$;

CREATE TRIGGER billing_period_transition_immutable
BEFORE UPDATE OR DELETE ON billing_core.billing_period_transition
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER billing_period_immutable
BEFORE UPDATE OR DELETE ON billing_core.billing_period
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER fx_rate_immutable
BEFORE UPDATE OR DELETE ON billing_core.fx_rate
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER fx_conversion_immutable
BEFORE UPDATE OR DELETE ON billing_core.fx_conversion
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_line_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_line
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_exception_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_exception
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_resolution_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_resolution
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

CREATE TRIGGER reconciliation_evidence_immutable
BEFORE UPDATE OR DELETE ON billing_core.reconciliation_evidence
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
