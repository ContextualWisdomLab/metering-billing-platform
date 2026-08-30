BEGIN;

ALTER TABLE billing_core.late_adjustment_application
    ADD CONSTRAINT late_adjustment_application_id_tenant_unique
    UNIQUE (late_adjustment_application_id, tenant_account_id);

CREATE TABLE billing_core.late_adjustment_rating (
    late_adjustment_rating_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    late_adjustment_application_id uuid NOT NULL,
    late_adjustment_id uuid NOT NULL,
    target_period_id uuid NOT NULL,
    adjustment_amount numeric NOT NULL CHECK (
        adjustment_amount <> 0
        AND adjustment_amount NOT IN (
            'NaN'::numeric,
            'Infinity'::numeric,
            '-Infinity'::numeric
        )
        AND length(adjustment_amount::text) <= 40
    ),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    rated_by text NOT NULL CHECK (btrim(rated_by) <> ''),
    authorization_reference text NOT NULL CHECK (btrim(authorization_reference) <> ''),
    rated_at timestamptz NOT NULL,
    late_adjustment_rating_contract_version integer NOT NULL CHECK (
        late_adjustment_rating_contract_version >= 1
    ),
    late_adjustment_rating_status text NOT NULL CHECK (
        late_adjustment_rating_status = 'rated'
    ),
    UNIQUE (tenant_account_id, late_adjustment_application_id),
    UNIQUE (tenant_account_id, late_adjustment_id),
    FOREIGN KEY (late_adjustment_application_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment_application
            (late_adjustment_application_id, tenant_account_id),
    FOREIGN KEY (late_adjustment_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment (late_adjustment_id, tenant_account_id),
    FOREIGN KEY (tenant_account_id, target_period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id)
);

CREATE INDEX late_adjustment_rating_tenant_rated_idx
    ON billing_core.late_adjustment_rating
        (tenant_account_id, rated_at, late_adjustment_rating_id);

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_rating()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    application billing_core.late_adjustment_application%ROWTYPE;
BEGIN
    -- Let ON CONFLICT classify generated-id and source replays before
    -- validating a row whose source period may have moved on.
    IF EXISTS (
        SELECT 1
        FROM billing_core.late_adjustment_rating AS existing
        WHERE existing.late_adjustment_rating_id = NEW.late_adjustment_rating_id
           OR (
               existing.tenant_account_id = NEW.tenant_account_id
               AND (
                   existing.late_adjustment_application_id = NEW.late_adjustment_application_id
                   OR existing.late_adjustment_id = NEW.late_adjustment_id
               )
           )
    ) THEN
        RETURN NEW;
    END IF;

    IF NEW.rated_at > clock_timestamp() THEN
        RAISE EXCEPTION 'late adjustment rating rated_at must not be in the future';
    END IF;

    SELECT *
    INTO application
    FROM billing_core.late_adjustment_application
    WHERE late_adjustment_application_id = NEW.late_adjustment_application_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment rating application is missing';
    END IF;
    IF NEW.late_adjustment_id <> application.late_adjustment_id THEN
        RAISE EXCEPTION 'late adjustment rating source does not match application';
    END IF;
    IF NEW.target_period_id <> application.target_period_id THEN
        RAISE EXCEPTION 'late adjustment rating target does not match application';
    END IF;
    IF NEW.adjustment_amount <> application.adjustment_amount THEN
        RAISE EXCEPTION 'late adjustment rating amount does not match application';
    END IF;
    IF NEW.currency_code <> application.currency_code THEN
        RAISE EXCEPTION 'late adjustment rating currency does not match application';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_rating_validate
BEFORE INSERT ON billing_core.late_adjustment_rating
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_late_adjustment_rating();

CREATE TRIGGER late_adjustment_rating_immutable
BEFORE UPDATE OR DELETE ON billing_core.late_adjustment_rating
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
