BEGIN;

ALTER TABLE billing_core.rate_card
    DROP CONSTRAINT IF EXISTS rate_card_rate_card_code_rate_card_version_key;

ALTER TABLE billing_core.rate_card
    ADD CONSTRAINT rate_card_tenant_code_version
        UNIQUE (tenant_account_id, rate_card_code, rate_card_version);

ALTER TABLE billing_core.billing_account
    ADD COLUMN billing_account_reference text;

UPDATE billing_core.billing_account AS account
SET billing_account_reference = tenant.tenant_reference || ':billing_account:' || account.billing_account_code
FROM billing_core.tenant_account AS tenant
WHERE tenant.tenant_account_id = account.tenant_account_id;

ALTER TABLE billing_core.billing_account
    ALTER COLUMN billing_account_reference SET NOT NULL,
    ADD CONSTRAINT billing_account_tenant_reference_unique
        UNIQUE (tenant_account_id, billing_account_reference);

ALTER TABLE billing_core.invoice_draft_line
    ADD COLUMN billing_account_reference text,
    ADD COLUMN meter_code text,
    ADD COLUMN unit_code text;

UPDATE billing_core.invoice_draft_line AS line
SET billing_account_reference = account.billing_account_reference,
    meter_code = meter.meter_code,
    unit_code = meter.unit_code
FROM billing_core.billing_account AS account,
     billing_core.meter_definition AS meter
WHERE account.tenant_account_id = line.tenant_account_id
  AND account.billing_account_id = line.billing_account_id
  AND meter.meter_definition_id = line.meter_definition_id;

ALTER TABLE billing_core.invoice_draft_line
    ALTER COLUMN billing_account_reference SET NOT NULL,
    ALTER COLUMN meter_code SET NOT NULL,
    ALTER COLUMN unit_code SET NOT NULL;

ALTER TABLE billing_core.rating_line
    ADD COLUMN billing_account_reference text,
    ADD COLUMN meter_code text,
    ADD COLUMN unit_code text,
    ADD COLUMN line_number integer;

UPDATE billing_core.rating_line AS line
SET billing_account_reference = tenant.tenant_reference || ':billing_account:' || account.billing_account_code,
    meter_code = meter.meter_code,
    unit_code = meter.unit_code,
    line_number = ranked.line_number
FROM billing_core.tenant_account AS tenant,
     billing_core.billing_account AS account,
     billing_core.meter_definition AS meter,
     (
         SELECT rating_line_id,
                row_number() OVER (
                    PARTITION BY rating_run_id
                    ORDER BY rating_line_id
                )::integer AS line_number
         FROM billing_core.rating_line
     ) AS ranked
WHERE account.tenant_account_id = line.tenant_account_id
  AND account.billing_account_id = line.billing_account_id
  AND tenant.tenant_account_id = line.tenant_account_id
  AND meter.meter_definition_id = line.meter_definition_id
  AND ranked.rating_line_id = line.rating_line_id;

ALTER TABLE billing_core.rating_line
    ALTER COLUMN billing_account_reference SET NOT NULL,
    ALTER COLUMN meter_code SET NOT NULL,
    ALTER COLUMN unit_code SET NOT NULL,
    ALTER COLUMN line_number SET NOT NULL,
    ADD CONSTRAINT rating_line_number_positive CHECK (line_number > 0),
    ADD CONSTRAINT rating_line_run_number_unique UNIQUE (rating_run_id, line_number);

ALTER TABLE billing_core.rating_run
    ADD COLUMN rate_card_version integer;

UPDATE billing_core.rating_run AS run
SET rate_card_version = card.rate_card_version
FROM billing_core.rate_card AS card
WHERE card.rate_card_id = run.rate_card_id;

ALTER TABLE billing_core.rating_run
    ALTER COLUMN rate_card_version SET NOT NULL,
    ADD CONSTRAINT rating_run_rate_card_version_positive CHECK (rate_card_version > 0);

ALTER TABLE billing_core.rating_run
    DROP CONSTRAINT rating_run_tenant_account_id_window_started_at_window_ended_key,
    ADD CONSTRAINT rating_run_tenant_window_card_version_snapshot_unique
        UNIQUE (
            tenant_account_id,
            window_started_at,
            window_ended_at,
            rate_card_id,
            rate_card_version,
            usage_snapshot_hash
        );

COMMIT;
