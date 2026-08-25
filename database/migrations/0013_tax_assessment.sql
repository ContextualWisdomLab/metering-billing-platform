BEGIN;

CREATE TABLE billing_core.tax_rate_schedule (
    tax_rate_schedule_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    tax_code text NOT NULL CHECK (tax_code IN ('vat', 'gst', 'sales_tax')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, tax_code),
    UNIQUE (tenant_account_id, tax_rate_schedule_id)
);

CREATE TABLE billing_core.tax_rate_version (
    tax_rate_version_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    tax_rate_schedule_id uuid NOT NULL,
    version_number integer NOT NULL CHECK (version_number >= 1),
    tax_rate_contract_version integer NOT NULL CHECK (tax_rate_contract_version >= 1),
    tax_code text NOT NULL CHECK (tax_code IN ('vat', 'gst', 'sales_tax')),
    tax_rate numeric(38, 12) NOT NULL CHECK (tax_rate >= 0 AND tax_rate <= 1),
    source_payload_hash text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, tax_rate_schedule_id, version_number),
    UNIQUE (tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version),
    UNIQUE (tenant_account_id, tax_rate_version_id),
    FOREIGN KEY (tenant_account_id, tax_rate_schedule_id)
        REFERENCES billing_core.tax_rate_schedule (tenant_account_id, tax_rate_schedule_id),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE billing_core.tax_assessment (
    tax_assessment_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    invoice_draft_id uuid NOT NULL,
    tax_rate_version_id uuid NOT NULL,
    tax_assessment_contract_version integer NOT NULL CHECK (tax_assessment_contract_version >= 1),
    tax_code text NOT NULL CHECK (tax_code IN ('vat', 'gst', 'sales_tax')),
    tax_rate numeric(38, 12) NOT NULL CHECK (tax_rate >= 0 AND tax_rate <= 1),
    currency_code text NOT NULL,
    tax_exclusive_amount numeric(38, 12) NOT NULL CHECK (tax_exclusive_amount > 0),
    tax_amount numeric(38, 12) NOT NULL CHECK (tax_amount >= 0),
    tax_inclusive_amount numeric(38, 12) NOT NULL CHECK (tax_inclusive_amount > 0),
    source_payload_hash text NOT NULL,
    assessed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, invoice_draft_id),
    UNIQUE (tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version),
    UNIQUE (tenant_account_id, tax_assessment_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id),
    FOREIGN KEY (tenant_account_id, tax_rate_version_id)
        REFERENCES billing_core.tax_rate_version (tenant_account_id, tax_rate_version_id),
    CHECK (tax_inclusive_amount = tax_exclusive_amount + tax_amount),
    CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')
);

COMMIT;
