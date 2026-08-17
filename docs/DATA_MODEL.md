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
- `rating_run`: append-only invoice-intent total for one tenant, half-open window, rate card, and usage snapshot.
- `rating_line`: append-only invoice-intent line for one billing account and meter inside a rating run.
- `invoice_draft`: append-only draft-only commercial document for one tenant and rating run.
- `invoice_draft_line`: append-only draft line copied from a rating line.
- `journal_proposal`: append-only balanced accounting-journal proposal for one tenant invoice draft.
- `journal_proposal_line`: append-only debit-or-credit line using a semantic account role.
- `collection_case`: append-only commercial collection case for one tenant invoice draft.
- `collection_dunning_event`: append-only commercial reminder that does not capture money.
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

Subsequent migrations add contracts, credits, spend reservations, issued invoices, payment intents, provider webhooks, refunds, disputes, settlements, and reconciliation exceptions without changing the initial identity, usage, rating-run, invoice-draft, journal-proposal, or collection-case keys.

## Usage identity

A stored usage row is identified twice: by `(tenant_account_id, source_event_key)` and by `(tenant_account_id, event_payload_hash, event_contract_version)`.  Measurements remain in their own table and reference the event and meter definition.  Time-window reads filter `occurred_at` and never leak another tenant's rows.

## Rating identity

A stored rating run is identified by `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)`.  Lines reference the run, tenant, billing account, and meter definition.  Money columns use exact `numeric` types.

## Invoice-draft identity

A stored invoice draft is identified by `(tenant_account_id, rating_run_id)` and carries the rating run's `usage_snapshot_hash`.  Status is `draft` only.  Lines reference the draft, tenant, billing account, and meter definition.

## Journal-proposal identity

A stored journal proposal is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)`.  Lines reference the proposal and tenant, carry unique `line_number` values, and must balance in the transaction currency.  Status is proposal-only.  Statutory account IDs and posted journals are not stored here.

## Collection-case identity

A stored collection case is identified by `(tenant_account_id, invoice_draft_id)`.  Outstanding is the exact invoice-draft total.  Status is `open` or `dunning` only.  Dunning events reference the case and tenant, carry unique notice codes and event numbers, and never capture payment or post journals.
