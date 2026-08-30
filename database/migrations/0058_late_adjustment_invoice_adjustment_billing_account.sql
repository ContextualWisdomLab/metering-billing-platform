BEGIN;

ALTER TABLE billing_core.late_adjustment_invoice_adjustment
    ADD COLUMN billing_account_id uuid,
    ADD COLUMN billing_account_reference text,
    ADD CONSTRAINT late_adjustment_invoice_adjustment_billing_account_pair_check
        CHECK ((billing_account_id IS NULL) = (billing_account_reference IS NULL)),
    ADD CONSTRAINT late_adjustment_invoice_adjustment_billing_account_fk
        FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id);

WITH account_counts AS (
    SELECT tenant_account_id, invoice_draft_id,
           count(DISTINCT billing_account_id) AS account_count
    FROM billing_core.invoice_draft_line
    GROUP BY tenant_account_id, invoice_draft_id
), one_account AS (
    SELECT DISTINCT ON (line.tenant_account_id, line.invoice_draft_id)
           line.tenant_account_id,
           line.invoice_draft_id,
           line.billing_account_id,
           account.billing_account_reference
    FROM billing_core.invoice_draft_line AS line
    JOIN account_counts AS counts
      ON counts.tenant_account_id = line.tenant_account_id
     AND counts.invoice_draft_id = line.invoice_draft_id
     AND counts.account_count = 1
    JOIN billing_core.billing_account AS account
      ON account.tenant_account_id = line.tenant_account_id
     AND account.billing_account_id = line.billing_account_id
    ORDER BY line.tenant_account_id, line.invoice_draft_id, line.line_number
)
UPDATE billing_core.late_adjustment_invoice_adjustment AS composition
SET billing_account_id = one_account.billing_account_id,
    billing_account_reference = one_account.billing_account_reference
FROM one_account
WHERE composition.tenant_account_id = one_account.tenant_account_id
  AND composition.invoice_draft_id = one_account.invoice_draft_id;

CREATE OR REPLACE FUNCTION billing_core.validate_late_adjustment_invoice_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    rating billing_core.late_adjustment_rating%ROWTYPE;
    draft billing_core.invoice_draft%ROWTYPE;
    account_reference text;
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
    SELECT account.billing_account_reference
    INTO account_reference
    FROM billing_core.billing_account AS account
    WHERE account.tenant_account_id = NEW.tenant_account_id
      AND account.billing_account_id = NEW.billing_account_id;
    IF NOT FOUND OR account_reference <> NEW.billing_account_reference THEN
        RAISE EXCEPTION 'late adjustment invoice adjustment billing account is invalid';
    END IF;
    IF (
        SELECT count(DISTINCT line.billing_account_id)
        FROM billing_core.invoice_draft_line AS line
        WHERE line.tenant_account_id = NEW.tenant_account_id
          AND line.invoice_draft_id = NEW.invoice_draft_id
    ) <> 1
       OR NOT EXISTS (
           SELECT 1
           FROM billing_core.invoice_draft_line AS line
           WHERE line.tenant_account_id = NEW.tenant_account_id
             AND line.invoice_draft_id = NEW.invoice_draft_id
             AND line.billing_account_id = NEW.billing_account_id
       ) THEN
        RAISE EXCEPTION 'invoice draft billing account is ambiguous';
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

COMMIT;
