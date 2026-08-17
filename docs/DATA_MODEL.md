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
- `rate_card`: tenant-scoped commercial price-book header identified by `(tenant_account_id, rate_card_name)`.
- `rate_card_version`: append-only published price list for one card. Identity is `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)`.
- `rate_card_line`: exact flat `unit_amount` for one `metric_code` on one published version. Currency must match the version.
- `rating_run`: append-only invoice-intent total for one tenant, half-open window, rate card, and usage snapshot.
- `rating_line`: append-only invoice-intent line for one billing account and meter inside a rating run.
- `invoice_draft`: append-only draft-only commercial document for one tenant and rating run.
- `invoice_draft_line`: append-only draft line copied from a rating line.
- `journal_proposal`: append-only balanced accounting-journal proposal for one tenant invoice draft, payment receipt, or credit adjustment. AIS pulls these rows; query does not add a second table.
- `journal_proposal_line`: append-only debit-or-credit line using a semantic account role.
- `collection_case`: commercial collection case for one tenant invoice draft; receipts update outstanding and may mark the case settled.
- `collection_dunning_event`: append-only commercial reminder that does not capture money.
- `payment_intent`: provider-neutral payment initiation projection for one collection case; cancellation updates current status.
- `payment_receipt`: append-only commercial receipt applied against one projected payment intent.
- `posting_receipt_observation`: append-only commercial observation of one AIS posting receipt. AIS `receipt_id` is an external reference, not the internal primary key.
- `credit_adjustment`: append-only commercial credit against one tenant invoice draft. The paired journal proposal reuses `journal_proposal`.
- `tax_rate_schedule`: tenant-scoped tax-rate header identified by `(tenant_account_id, tax_code)`.
- `tax_rate_version`: append-only published tax rate. Identity is `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)`.
- `tax_assessment`: append-only commercial tax on one tenant invoice draft. `tax_inclusive_amount` drives collection outstanding and the AR journal debit when present.
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

Subsequent migrations add contracts, spend reservations, issued invoices, provider webhooks, refunds, disputes, and reconciliation exceptions without changing the initial identity, usage, rating-run, invoice-draft, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, rate-card-catalog, tax-assessment, or credit-tax-unwind keys.

## Usage identity

A stored usage row is identified twice: by `(tenant_account_id, source_event_key)` and by `(tenant_account_id, event_payload_hash, event_contract_version)`.  Measurements remain in their own table and reference the event and meter definition.  Time-window reads filter `occurred_at` and never leak another tenant's rows.

## Rating identity

A stored rating run is identified by `(tenant_account_id, window_started_at, window_ended_at, rate_card_version_id, usage_snapshot_hash)`.  The run pins the published version so a later catalog publish cannot rewrite earlier invoice-intent money.  Lines reference the run, tenant, billing account, and meter definition.  Money columns use exact `numeric` types.

## Rate-card identity

A stored rate-card header is identified by `(tenant_account_id, rate_card_name)`.  Internal primary key is `rate_card_id`.  A stored version is identified by `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)` and also by `(tenant_account_id, rate_card_id, version_number)`.  Lines reference the version and tenant, carry unique `metric_code` values, and store exact `unit_amount` values greater than zero.  A published version is never updated.

## Invoice-draft identity

A stored invoice draft is identified by `(tenant_account_id, rating_run_id)` and carries the rating run's `usage_snapshot_hash`.  Status is `draft` only.  Lines reference the draft, tenant, billing account, and meter definition.

## Journal-proposal identity

A stored journal proposal is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)` for invoice-draft exports, by `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)` for cash exports, and by `(tenant_account_id, credit_adjustment_id, source_payload_hash, proposal_contract_version)` for credit exports.  Lines reference the proposal and tenant, carry unique `line_number` values, and must balance in the transaction currency.  Untaxed draft lines use semantic `accounts_receivable` and `usage_revenue` roles.  Taxed draft lines add semantic `tax_payable`.  Cash lines use semantic `cash_receipt` and `accounts_receivable` roles.  Credit lines use semantic `usage_revenue` and `accounts_receivable` roles.  Status is proposal-only.  Statutory account IDs and posted journals are not stored here.

## Collection-case identity

A stored collection case is identified by `(tenant_account_id, invoice_draft_id)`.  Outstanding starts as `tax_inclusive_amount` when a tax assessment exists, otherwise the exact invoice-draft total.  Status is `open` or `dunning` until applied receipts or commercial credits reduce outstanding to zero and mark the case `settled`.  Dunning events reference the case and tenant, carry unique notice codes and event numbers, and never capture payment or post journals.

## Payment-intent identity

A stored payment intent is identified by `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  The hash covers the case outstanding, currency, and stored status snapshot.  Status is `projected`, `cancelled`, or `rejected` only.  Provider charge IDs and card PAN are not stored.

## Payment-receipt identity

A stored payment receipt is identified by `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  The hash covers the intent amount, currency, status, and received amount.  Status is `applied` only.  Provider charge IDs and card PAN are not stored.  Receipts do not emit an `accounting_journal_proposal`.

## Posting-receipt observation identity

A stored posting-receipt observation is identified by `(tenant_account_id, idempotency_key)` plus `source_payload_hash` and AIS `receipt_id`.  Internal primary key is `posting_receipt_observation_id`.  `posting_status_code` is AIS-owned (`posted`, `held`, `rejected`, `reversed`) and is not mapped onto journal `proposal_status`.

## Credit-adjustment identity

A stored credit adjustment is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)`.  Internal primary key is `credit_adjustment_id`.  The hash covers the draft, exact credit amount, closed reason, currency, and the tax split when the draft is taxed.  Status is `recorded` only.  Remaining adjustable is the tax-inclusive amount when an assessment exists, otherwise the draft total, minus prior accepted credits.  If a collection case exists, outstanding is reduced by the same inclusive amount and cannot go negative.  `tax_exclusive_amount` plus `tax_amount` equals `credit_amount`.  A taxed journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`.

## Tax-assessment identity

A stored tax-rate schedule is identified by `(tenant_account_id, tax_code)`.  A stored version is identified by `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)` and also by `(tenant_account_id, tax_rate_schedule_id, version_number)`.  A stored assessment is identified by `(tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version)` and is unique per draft.  `tax_amount` is half-even rounded to the documented ISO 4217 minor-unit exponent.
