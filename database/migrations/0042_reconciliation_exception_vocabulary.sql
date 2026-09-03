BEGIN;

ALTER TABLE billing_core.reconciliation_exception
    DROP CONSTRAINT reconciliation_exception_exception_code_check,
    ADD CONSTRAINT reconciliation_exception_exception_code_check CHECK (exception_code IN (
        'quantity_mismatch',
        'price_mismatch',
        'tax_mismatch',
        'currency_mismatch',
        'payment_missing',
        'duplicate_charge',
        'refund_mismatch',
        'dispute_mismatch',
        'settlement_mismatch',
        'provider_fee_mismatch',
        'cash_timing_difference',
        'unmapped_provider_object'
    ));

ALTER TABLE billing_core.reconciliation_resolution
    DROP CONSTRAINT reconciliation_resolution_exception_code_check,
    ADD CONSTRAINT reconciliation_resolution_exception_code_check CHECK (exception_code IN (
        'quantity_mismatch',
        'price_mismatch',
        'tax_mismatch',
        'currency_mismatch',
        'payment_missing',
        'duplicate_charge',
        'refund_mismatch',
        'dispute_mismatch',
        'settlement_mismatch',
        'provider_fee_mismatch',
        'cash_timing_difference',
        'unmapped_provider_object'
    ));

COMMIT;
