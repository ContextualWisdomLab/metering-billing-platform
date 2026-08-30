BEGIN;

CREATE INDEX late_adjustment_keyset_idx
    ON billing_core.late_adjustment (
        tenant_account_id,
        recorded_at,
        late_adjustment_id
    );

COMMIT;
