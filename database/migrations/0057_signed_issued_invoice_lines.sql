BEGIN;

ALTER TABLE billing_core.late_adjustment_invoice_adjustment
    ADD CONSTRAINT late_adjustment_invoice_adjustment_id_tenant_unique
    UNIQUE (late_adjustment_invoice_adjustment_id, tenant_account_id);

ALTER TABLE billing_core.issued_invoice_line
    ADD COLUMN line_type text NOT NULL DEFAULT 'usage' CHECK (
        line_type IN ('usage', 'late_adjustment')
    ),
    ADD COLUMN late_adjustment_invoice_adjustment_id uuid,
    ADD CONSTRAINT issued_invoice_line_late_adjustment_fk
        FOREIGN KEY (tenant_account_id, late_adjustment_invoice_adjustment_id)
        REFERENCES billing_core.late_adjustment_invoice_adjustment
            (tenant_account_id, late_adjustment_invoice_adjustment_id),
    ADD CONSTRAINT issued_invoice_line_late_adjustment_identity_unique
        UNIQUE (tenant_account_id, late_adjustment_invoice_adjustment_id),
    DROP CONSTRAINT issued_invoice_line_line_total_amount_check,
    ADD CONSTRAINT issued_invoice_line_amounts_check CHECK (
        (
            line_type = 'usage'
            AND rated_quantity >= 0
            AND unit_price_amount >= 0
            AND line_total_amount >= 0
            AND late_adjustment_invoice_adjustment_id IS NULL
        )
        OR (
            line_type = 'late_adjustment'
            AND rated_quantity = 1
            AND unit_price_amount >= 0
            AND line_total_amount <> 0
            AND late_adjustment_invoice_adjustment_id IS NOT NULL
        )
    );

COMMIT;
