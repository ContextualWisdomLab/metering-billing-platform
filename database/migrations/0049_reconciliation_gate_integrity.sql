BEGIN;

CREATE OR REPLACE FUNCTION billing_core.assert_reconciliation_gate(
    p_tenant_account_id uuid,
    p_period_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    latest_blocking_count integer;
    latest_exception_count bigint;
    latest_unresolved_count bigint;
    latest_run_line_count bigint;
    period_line_count bigint;
BEGIN
    SELECT run.blocking_exception_count,
           COUNT(exception.exception_code),
           COUNT(exception.exception_code) FILTER (
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM billing_core.reconciliation_resolution AS resolution
                   WHERE resolution.reconciliation_line_id = exception.reconciliation_line_id
                     AND resolution.exception_code = exception.exception_code
                     AND resolution.resolution_status IN ('resolved', 'waived')
               )
           ),
           COUNT(DISTINCT run_line.reconciliation_line_id),
           (
               SELECT COUNT(*)
               FROM billing_core.reconciliation_line AS period_line
               WHERE period_line.tenant_account_id = run.tenant_account_id
                 AND period_line.period_id = run.period_id
           )
    INTO latest_blocking_count,
         latest_exception_count,
         latest_unresolved_count,
         latest_run_line_count,
         period_line_count
    FROM billing_core.reconciliation_run AS run
    LEFT JOIN billing_core.reconciliation_run_line AS run_line
      ON run_line.run_id = run.run_id
    LEFT JOIN billing_core.reconciliation_exception AS exception
      ON exception.reconciliation_line_id = run_line.reconciliation_line_id
    WHERE run.tenant_account_id = p_tenant_account_id
      AND run.period_id = p_period_id
    GROUP BY run.run_id, run.blocking_exception_count, run.completed_at
    ORDER BY run.completed_at DESC, run.run_id DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'a completed reconciliation run is required';
    END IF;
    IF latest_blocking_count <> latest_exception_count THEN
        RAISE EXCEPTION 'reconciliation run exception summary is inconsistent';
    END IF;
    IF latest_run_line_count <> period_line_count THEN
        RAISE EXCEPTION 'reconciliation run does not cover every period line';
    END IF;
    IF latest_unresolved_count <> 0 THEN
        RAISE EXCEPTION 'blocking reconciliation exceptions remain unresolved';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION billing_core.validate_billing_period_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_transition_number integer := 0;
    current_status text := 'open';
BEGIN
    PERFORM 1
    FROM billing_core.billing_period
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.period_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'billing period does not exist for transition';
    END IF;

    SELECT transition_number, to_status
    INTO current_transition_number, current_status
    FROM billing_core.billing_period_transition
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.period_id
    ORDER BY transition_number DESC
    LIMIT 1;

    IF current_transition_number IS NULL THEN
        current_transition_number := 0;
        current_status := 'open';
    END IF;
    IF NEW.transition_number <> current_transition_number + 1
       OR NEW.from_status IS DISTINCT FROM current_status
    THEN
        RAISE EXCEPTION 'billing period transition is not the next lifecycle step';
    END IF;
    IF NEW.to_status = 'reconciled' THEN
        PERFORM billing_core.assert_reconciliation_gate(
            NEW.tenant_account_id,
            NEW.period_id
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER billing_period_transition_gate
BEFORE INSERT ON billing_core.billing_period_transition
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_billing_period_transition();

CREATE OR REPLACE FUNCTION billing_core.reject_reconciliation_line_after_close()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_status text;
BEGIN
    PERFORM 1
    FROM billing_core.billing_period
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.period_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'billing period does not exist for reconciliation line';
    END IF;

    SELECT to_status
    INTO current_status
    FROM billing_core.billing_period_transition
    WHERE tenant_account_id = NEW.tenant_account_id
      AND period_id = NEW.period_id
    ORDER BY transition_number DESC
    LIMIT 1;
    IF current_status IN ('reconciled', 'invoiced', 'hard_closed') THEN
        RAISE EXCEPTION 'reconciliation lines cannot be added after period reconciliation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER reconciliation_line_period_gate
BEFORE INSERT ON billing_core.reconciliation_line
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_reconciliation_line_after_close();

COMMIT;
