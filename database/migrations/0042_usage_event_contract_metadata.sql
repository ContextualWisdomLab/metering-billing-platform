BEGIN;

ALTER TABLE billing_core.usage_event
    ADD COLUMN producer_contract_version integer NOT NULL DEFAULT 1,
    ADD COLUMN repository_reference text,
    ADD COLUMN trace_reference text,
    ADD COLUMN correlation_reference text,
    ADD COLUMN causation_reference text,
    ADD COLUMN available_at timestamptz,
    ADD COLUMN correction_lineage jsonb;

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_producer_contract_version_positive
        CHECK (producer_contract_version > 0),
    ADD CONSTRAINT usage_event_correction_lineage_object
        CHECK (
            correction_lineage IS NULL
            OR (
                jsonb_typeof(correction_lineage) = 'object'
                AND correction_lineage ? 'prior_event_id'
                AND correction_lineage ? 'relationship_code'
                AND correction_lineage->>'prior_event_id' ~
                    '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
                AND correction_lineage->>'relationship_code' IN ('corrects', 'reverses', 'supersedes')
            )
        );

COMMIT;
