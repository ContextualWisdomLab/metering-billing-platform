BEGIN;

ALTER TABLE billing_core.usage_event
    RENAME COLUMN dimensions TO usage_dimensions;

ALTER TABLE billing_core.usage_event
    RENAME CONSTRAINT usage_event_dimensions_object
    TO usage_event_usage_dimensions_object;

COMMIT;
