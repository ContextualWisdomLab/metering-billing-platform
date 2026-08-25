BEGIN;

ALTER TABLE billing_core.collection_dispute
    DROP CONSTRAINT collection_dispute_collection_dispute_status_check,
    ADD CONSTRAINT collection_dispute_collection_dispute_status_check
        CHECK (collection_dispute_status IN ('held', 'released'));

ALTER TABLE billing_core.collection_dispute
    ADD COLUMN released_at timestamptz;

ALTER TABLE billing_core.collection_dispute
    ADD CONSTRAINT collection_dispute_released_at_check
        CHECK (
            (
                collection_dispute_status = 'held'
                AND released_at IS NULL
            )
            OR (
                collection_dispute_status = 'released'
                AND released_at IS NOT NULL
            )
        );

COMMIT;
