BEGIN;

ALTER TABLE billing_core.usage_event
    ADD COLUMN usage_dimensions jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_usage_dimensions_object
    CHECK (jsonb_typeof(usage_dimensions) = 'object');

COMMIT;
