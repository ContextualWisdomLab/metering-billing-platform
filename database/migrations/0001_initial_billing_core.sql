BEGIN;

CREATE SCHEMA IF NOT EXISTS billing_core;

CREATE TABLE billing_core.tenant_account (
    tenant_account_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_code text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE billing_core.billing_account (
    billing_account_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    billing_account_code text NOT NULL,
    account_status_code text NOT NULL CHECK (account_status_code IN ('active', 'suspended', 'closed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, billing_account_code),
    UNIQUE (tenant_account_id, billing_account_id)
);

CREATE TABLE billing_core.billing_principal (
    billing_principal_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    principal_kind_code text NOT NULL CHECK (
        principal_kind_code IN ('human_user', 'service_account', 'agent_identity', 'github_workflow', 'application_runtime', 'integration_client')
    ),
    principal_reference text NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, principal_reference, valid_from),
    UNIQUE (tenant_account_id, billing_principal_id),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE billing_core.credential_record (
    credential_record_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    credential_kind_code text NOT NULL CHECK (
        credential_kind_code IN ('api_key', 'personal_access_token', 'oauth_client', 'service_token', 'service_account', 'iap_identity', 'agent_delegation')
    ),
    credential_fingerprint text NOT NULL,
    issuer_reference text NOT NULL,
    issued_at timestamptz NOT NULL,
    revoked_at timestamptz,
    UNIQUE (tenant_account_id, credential_fingerprint),
    UNIQUE (tenant_account_id, credential_record_id),
    CHECK (revoked_at IS NULL OR revoked_at >= issued_at)
);

CREATE TABLE billing_core.credential_assignment (
    credential_assignment_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    credential_record_id uuid NOT NULL,
    billing_principal_id uuid NOT NULL,
    billing_account_id uuid NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (credential_record_id, valid_from),
    FOREIGN KEY (tenant_account_id, credential_record_id)
        REFERENCES billing_core.credential_record (tenant_account_id, credential_record_id),
    FOREIGN KEY (tenant_account_id, billing_principal_id)
        REFERENCES billing_core.billing_principal (tenant_account_id, billing_principal_id),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE billing_core.meter_definition (
    meter_definition_id uuid PRIMARY KEY DEFAULT uuidv7(),
    meter_code text NOT NULL,
    meter_version integer NOT NULL CHECK (meter_version > 0),
    unit_code text NOT NULL,
    aggregation_code text NOT NULL CHECK (aggregation_code IN ('sum', 'maximum', 'most_recent', 'distinct_count')),
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    UNIQUE (meter_code, meter_version),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE billing_core.meter_quality_rule (
    meter_quality_rule_id uuid PRIMARY KEY DEFAULT uuidv7(),
    meter_definition_id uuid NOT NULL REFERENCES billing_core.meter_definition (meter_definition_id),
    quality_code text NOT NULL CHECK (
        quality_code IN ('provider_reported', 'locally_measured', 'deterministically_derived', 'estimated', 'reconstructed', 'corrected')
    ),
    billing_disposition_code text NOT NULL CHECK (billing_disposition_code IN ('billable', 'analytics_only', 'manual_review')),
    UNIQUE (meter_definition_id, quality_code)
);

CREATE TABLE billing_core.usage_event (
    usage_event_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    billing_account_id uuid NOT NULL,
    billing_principal_id uuid NOT NULL,
    credential_record_id uuid,
    source_event_key text NOT NULL,
    product_code text NOT NULL,
    operation_code text,
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    event_payload_hash text NOT NULL,
    UNIQUE (tenant_account_id, source_event_key),
    FOREIGN KEY (tenant_account_id, billing_account_id)
        REFERENCES billing_core.billing_account (tenant_account_id, billing_account_id),
    FOREIGN KEY (tenant_account_id, billing_principal_id)
        REFERENCES billing_core.billing_principal (tenant_account_id, billing_principal_id),
    FOREIGN KEY (tenant_account_id, credential_record_id)
        REFERENCES billing_core.credential_record (tenant_account_id, credential_record_id)
);

CREATE TABLE billing_core.usage_measurement (
    usage_measurement_id uuid PRIMARY KEY DEFAULT uuidv7(),
    usage_event_id uuid NOT NULL REFERENCES billing_core.usage_event (usage_event_id),
    meter_definition_id uuid NOT NULL REFERENCES billing_core.meter_definition (meter_definition_id),
    measured_quantity numeric(38, 12) NOT NULL CHECK (measured_quantity >= 0),
    quality_code text NOT NULL CHECK (
        quality_code IN ('provider_reported', 'locally_measured', 'deterministically_derived', 'estimated', 'reconstructed', 'corrected')
    ),
    UNIQUE (usage_event_id, meter_definition_id),
    FOREIGN KEY (meter_definition_id, quality_code)
        REFERENCES billing_core.meter_quality_rule (meter_definition_id, quality_code)
);

CREATE TABLE billing_core.provider_account (
    provider_account_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid REFERENCES billing_core.tenant_account (tenant_account_id),
    provider_code text NOT NULL,
    provider_role_code text NOT NULL CHECK (
        provider_role_code IN ('merchant_of_record', 'payment_processor', 'payment_gateway', 'payment_orchestrator', 'tax_service', 'invoicing_service', 'metering_service', 'manual_collection')
    ),
    account_status_code text NOT NULL CHECK (account_status_code IN ('active', 'disabled', 'retired')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE billing_core.provider_capability (
    provider_capability_id uuid PRIMARY KEY DEFAULT uuidv7(),
    provider_account_id uuid NOT NULL REFERENCES billing_core.provider_account (provider_account_id),
    capability_code text NOT NULL,
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    UNIQUE (provider_account_id, capability_code, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE billing_core.provider_object_mapping (
    provider_object_mapping_id uuid PRIMARY KEY DEFAULT uuidv7(),
    provider_account_id uuid NOT NULL REFERENCES billing_core.provider_account (provider_account_id),
    internal_object_type text NOT NULL,
    internal_object_reference text NOT NULL,
    provider_object_type text NOT NULL,
    provider_object_reference text NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (provider_account_id, provider_object_type, provider_object_reference, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE billing_core.accounting_export_record (
    accounting_export_record_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    proposal_reference text NOT NULL,
    idempotency_key text NOT NULL,
    proposal_status_code text NOT NULL CHECK (proposal_status_code IN ('draft', 'validated', 'exported', 'rejected')),
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    exported_at timestamptz,
    UNIQUE (tenant_account_id, idempotency_key)
);

CREATE TABLE billing_core.outbox_event (
    outbox_event_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid REFERENCES billing_core.tenant_account (tenant_account_id),
    event_type_code text NOT NULL,
    aggregate_reference text NOT NULL,
    payload_reference text NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz
);

COMMIT;
