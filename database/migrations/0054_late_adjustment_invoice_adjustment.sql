BEGIN;

ALTER TABLE billing_core.late_adjustment_rating
    ADD CONSTRAINT late_adjustment_rating_id_tenant_unique
    UNIQUE (late_adjustment_rating_id, tenant_account_id);

CREATE TABLE billing_core.late_adjustment_invoice_adjustment (
    late_adjustment_invoice_adjustment_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    late_adjustment_rating_id uuid NOT NULL,
    late_adjustment_application_id uuid NOT NULL,
    late_adjustment_id uuid NOT NULL,
    invoice_draft_id uuid NOT NULL,
    target_period_id uuid NOT NULL,
    adjustment_amount numeric NOT NULL CHECK (
        adjustment_amount <> 0
        AND adjustment_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
        AND length(adjustment_amount::text) <= 40
    ),
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    recorded_by text NOT NULL CHECK (btrim(recorded_by) <> ''),
    authorization_reference text NOT NULL CHECK (btrim(authorization_reference) <> ''),
    recorded_at timestamptz NOT NULL,
    source_payload_hash text NOT NULL CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    late_adjustment_invoice_adjustment_contract_version integer NOT NULL CHECK (
        late_adjustment_invoice_adjustment_contract_version >= 1
    ),
    late_adjustment_invoice_adjustment_status text NOT NULL CHECK (
        late_adjustment_invoice_adjustment_status = 'recorded'
    ),
    UNIQUE (tenant_account_id, late_adjustment_rating_id),
    FOREIGN KEY (late_adjustment_rating_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment_rating
            (late_adjustment_rating_id, tenant_account_id),
    FOREIGN KEY (late_adjustment_application_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment_application
            (late_adjustment_application_id, tenant_account_id),
    FOREIGN KEY (late_adjustment_id, tenant_account_id)
        REFERENCES billing_core.late_adjustment (late_adjustment_id, tenant_account_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, target_period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id)
);

CREATE INDEX late_adjustment_invoice_adjustment_tenant_recorded_idx
    ON billing_core.late_adjustment_invoice_adjustment
        (tenant_account_id, recorded_at, late_adjustment_invoice_adjustment_id);

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_invoice_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rating billing_core.late_adjustment_rating%ROWTYPE;
    draft billing_core.invoice_draft%ROWTYPE;
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

    SELECT * INTO rating
    FROM billing_core.late_adjustment_rating
    WHERE late_adjustment_rating_id = NEW.late_adjustment_rating_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment invoice adjustment rating is missing';
    END IF;
    SELECT * INTO draft
    FROM billing_core.invoice_draft
    WHERE invoice_draft_id = NEW.invoice_draft_id
      AND tenant_account_id = NEW.tenant_account_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'late adjustment invoice adjustment draft is missing';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM billing_core.issued_invoice
        WHERE tenant_account_id = NEW.tenant_account_id
          AND invoice_draft_id = NEW.invoice_draft_id
    ) THEN
        RAISE EXCEPTION 'invoice draft already has an issued invoice';
    END IF;
    IF NEW.late_adjustment_application_id <> rating.late_adjustment_application_id
       OR NEW.late_adjustment_id <> rating.late_adjustment_id
       OR NEW.target_period_id <> rating.target_period_id
       OR NEW.adjustment_amount <> rating.adjustment_amount
       OR NEW.currency_code <> rating.currency_code
       OR draft.currency_code <> rating.currency_code THEN
        RAISE EXCEPTION 'late adjustment invoice adjustment does not match evidence';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER late_adjustment_invoice_adjustment_validate
BEFORE INSERT ON billing_core.late_adjustment_invoice_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.validate_late_adjustment_invoice_adjustment();

CREATE TRIGGER late_adjustment_invoice_adjustment_immutable
BEFORE UPDATE OR DELETE ON billing_core.late_adjustment_invoice_adjustment
FOR EACH ROW EXECUTE FUNCTION billing_core.reject_immutable_reconciliation_fact_mutation();

COMMIT;
