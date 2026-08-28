BEGIN;

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing_core'
          AND table_name = 'usage_event'
          AND column_name = 'dimensions'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'billing_core'
          AND table_name = 'usage_event'
          AND column_name = 'usage_dimensions'
    ) THEN
        EXECUTE 'ALTER TABLE billing_core.usage_event
                 RENAME COLUMN dimensions TO usage_dimensions';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_schema = 'billing_core'
          AND table_name = 'usage_event'
          AND constraint_name = 'usage_event_dimensions_object'
    ) THEN
        EXECUTE 'ALTER TABLE billing_core.usage_event
                 RENAME CONSTRAINT usage_event_dimensions_object
                 TO usage_event_usage_dimensions_object';
    END IF;
END
$migration$;

COMMIT;
