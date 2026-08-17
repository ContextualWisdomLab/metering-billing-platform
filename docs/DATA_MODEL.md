# Data Model

## Initial normalized records

- `tenant_account`: tenant authority boundary.
- `billing_account`: commercial payer and invoice grouping.
- `billing_principal`: human, service, agent, workflow, or runtime attribution subject.
- `credential_record`: opaque fingerprint and issuer reference; no plaintext secret.
- `credential_assignment`: effective-dated link among credential, principal, and billing account.
- `meter_definition`: versioned unit and aggregation rule.
- `meter_quality_rule`: billable, analytics-only, or manual-review disposition by quality.
- `usage_event`: idempotent source fact identified by tenant-scoped `source_event_key` and by `(tenant_account_id, event_payload_hash, event_contract_version)`. The producer `event_id` is stored as `producer_event_id`, not as the internal primary key.
- `usage_measurement`: normalized meter quantity and quality, constrained to an explicit meter-specific quality rule.
- `usage_ingestion_receipt`: append-only accepted, replay, or rejected outcome for every ingest attempt, including rejected cross-tenant and schema failures.
- `rate_card`: versioned commercial price book and currency.
- `rate_card_price`: exact unit price for one meter on one rate-card version.
- `rating_run`: append-only invoice-intent identity for one tenant, half-open window, rate-card version, and usage snapshot.
- `rating_line`: exact billed quantity, unit price, and line amount for one meter inside a rating run.
- `provider_account`: provider and role registration.
- `provider_capability`: effective-dated supported capability.
- `provider_object_mapping`: provider-neutral internal-to-external mapping.
- `accounting_export_record`: proposal lifecycle and payload integrity.
- `outbox_event`: transactional publication record.

## Temporal rule

Assignments and capabilities use `valid_from`, `valid_to`, and `recorded_at`. Closing an interval supersedes a fact; it does not erase history. Composite foreign keys bind credentials, principals, billing accounts, and usage to the same tenant.

## Monetary rule

Database numeric values use exact `numeric` types. API amounts use canonical decimal strings. Binary floating-point types are forbidden for quantities that affect billing or accounting.

## Future extensions

Subsequent migrations add contracts, credits, spend reservations, invoice drafts, provider webhooks, refunds, disputes, settlements, and reconciliation exceptions without changing the usage or rating-run keys.

## Usage identity

A stored usage row is identified twice: by `(tenant_account_id, source_event_key)` and by `(tenant_account_id, event_payload_hash, event_contract_version)`.  Measurements remain in their own table and reference the event and meter definition.  Time-window reads filter `occurred_at` and never leak another tenant's rows.

## Rating identity

A rating run is identified by `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)`.  `rate_card_id` names one `(rate_card_code, rate_card_version)` row.  Lines reference the run through a tenant-scoped composite foreign key.  Invoice-intent totals are exact `numeric` values.
