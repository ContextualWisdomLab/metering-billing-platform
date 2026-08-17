BEGIN;

ALTER TABLE billing_core.credit_adjustment
    ADD COLUMN tax_exclusive_amount numeric(38, 12) NOT NULL DEFAULT 0
        CHECK (tax_exclusive_amount >= 0);

ALTER TABLE billing_core.credit_adjustment
    ADD COLUMN tax_amount numeric(38, 12) NOT NULL DEFAULT 0
        CHECK (tax_amount >= 0);

ALTER TABLE billing_core.credit_adjustment
    ADD CONSTRAINT credit_adjustment_tax_split_check
        CHECK (tax_exclusive_amount + tax_amount = credit_amount);

COMMIT;
