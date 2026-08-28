BEGIN;

ALTER TABLE billing_core.usage_event
    VALIDATE CONSTRAINT usage_event_usage_dimensions_object;

ALTER TABLE billing_core.usage_event
    VALIDATE CONSTRAINT usage_event_producer_contract_version_positive;

ALTER TABLE billing_core.usage_event
    VALIDATE CONSTRAINT usage_event_correction_lineage_object;

COMMIT;
