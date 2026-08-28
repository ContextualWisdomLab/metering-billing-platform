BEGIN;

ALTER TABLE billing_core.usage_event
    DROP CONSTRAINT usage_event_correction_lineage_object;

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_correction_lineage_object
        CHECK (
            correction_lineage IS NULL
            OR (
                jsonb_typeof(correction_lineage) = 'object'
                AND correction_lineage ? 'prior_event_id'
                AND correction_lineage ? 'relationship_code'
                AND correction_lineage->>'prior_event_id' ~
                    '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
                AND correction_lineage->>'relationship_code' IN ('corrects', 'reverses', 'supersedes')
            )
        );

COMMIT;
