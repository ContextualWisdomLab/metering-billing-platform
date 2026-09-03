BEGIN;

ALTER TABLE billing_core.usage_event
    ADD COLUMN dimensions jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE billing_core.usage_event
    ADD CONSTRAINT usage_event_dimensions_object
    CHECK (jsonb_typeof(dimensions) = 'object') NOT VALID;

COMMIT;
