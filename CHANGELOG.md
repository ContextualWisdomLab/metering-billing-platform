# Changelog

## [Unreleased]

### Added

- `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}` let AIS pull persisted proposals without mutating `proposal_status`.  Cash and AR proposals share the same list.  Tenant is required.  HTTP 200 is a successful read; HTTP 422 is a missing tenant or illegal filter; HTTP 404 is an unknown or cross-tenant proposal.
- ADR 0012 for query-without-mutation and AIS pull.
- Stdlib HTTP accept surface (`python -m metering_billing.http_app` or `create_http_app(ledger=...)`) exposes the existing commercial services as JSON.  Money stays exact-decimal strings.  Tenant is required on every write.  HTTP 200 means `accepted` or `duplicate_replay`; HTTP 422 means `rejected`; HTTP 404 is only an unknown route.
- ADR 0011 for HTTP as a thin adapter that does not post journals or add a named payment provider.
- `AccountingExportService.propose_cash_journal` emits one balanced cash/AR `accounting_journal_proposal` from a stored payment receipt.
- Idempotent cash-proposal identity on `(tenant, payment_receipt_id, source_payload_hash, proposal_contract_version)` so a replay returns the same `proposal_id`.
- AIS-compatible cash idempotency key `{tenant}:cash_receipt:{payment_receipt_id}:{source_payload_hash}:v{version}`.
- ADR 0010 for cash journal export without posting, fiscal-period control, or statutory account IDs.
- Importable `metering_billing` payment-settlement service that applies an exact commercial receipt against a projected payment intent and reduces collection outstanding.
- Idempotent payment-receipt identity on `(tenant, payment_intent_id, source_payload_hash, settlement_contract_version)` so a replay returns the same `payment_receipt_id`.
- Append-only `payment_receipt` persistence.  Status stays `applied`.  A full receipt settles the collection case; a partial receipt leaves residual outstanding.
- `cancel_payment_intent` flips a projected intent to `cancelled` without writing a receipt or changing outstanding.
- ADR 0009 for commercial settlement without provider capture, PAN storage, cash journal export, or AIS posting.
- Importable `metering_billing` payment-intent service that projects a provider-neutral initiation record from a stored collection case.
- Idempotent payment-intent identity on `(tenant, collection_case_id, source_payload_hash, payment_intent_contract_version)` so a replay returns the same `payment_intent_id`.
- Append-only `payment_intent` persistence.  Status stays `projected`, `cancelled`, or `rejected`.
- ADR 0008 for projected payment initiation without capture, settlement, PAN storage, or journal posting.
- Importable `metering_billing` collection-case service that opens a tenant-scoped commercial case from a stored invoice draft and appends dunning reminders.
- Idempotent collection-case identity on `(tenant, invoice_draft_id)` so a replay returns the same `collection_case_id` and exact outstanding.
- Append-only `collection_case` and `collection_dunning_event` persistence.  Status stays `open` or `dunning`.
- ADR 0007 for commercial collection without payment capture or journal posting.
- Importable `metering_billing` accounting-export service that copies a stored invoice draft into one balanced, exact-decimal `accounting_journal_proposal`.
- Idempotent journal-proposal identity on `(tenant, invoice_draft_id, source_payload_hash, proposal_contract_version)` so a replay returns the same `proposal_id`.
- Append-only `journal_proposal` and `journal_proposal_line` persistence that keeps debit totals equal to credit totals.
- ADR 0006 for proposal-only journal export; AIS remains the posting consumer.
- Importable `metering_billing` invoice-draft service that copies a stored rating run into an exact, tenant-scoped, draft-only invoice-intent document.
- Idempotent invoice-draft identity on `(tenant, rating_run_id)` so a replay returns the same `invoice_draft_id` and totals.
- Append-only `invoice_draft` and `invoice_draft_line` persistence and a closed invoice-draft contract.
- ADR 0005 for invoice drafts as commercial documents, not statutory invoices.
- Importable `metering_billing` rating service that turns a tenant plus half-open ISO 8601 window into exact invoice-intent line totals from already-stored usage.
- Idempotent rating-run identity on `(tenant, window, rate_card_version, usage snapshot)` so a replay returns the same `rating_run_id` and totals.
- Append-only `rating_run` and `rating_line` persistence, versioned `rate_card` prices, and a closed rating-run contract.
- Billable-only rating against `meter_quality_rule`; analytics-only and manual-review measurements stay out of invoice-intent money.
- ADR 0004 for deterministic time-windowed rating.
- Importable `metering_billing` usage-ingestion service with tenant-scoped attribution, exact-decimal measurements, and half-open ISO 8601 query windows.
- Idempotent deduplication on `(tenant, source_event_key)` and `(tenant, source_payload_hash, event_contract_version)`.
- Internal `usage_event_id` generation so a reused producer `event_id` cannot overwrite stored usage.
- Append-only `usage_ingestion_receipt` rows for every accept, replay, and reject.
- Usage-ingestion receipt contract and PostgreSQL migration for contract version, payload-hash uniqueness, and ingestion receipts.
- Receipt contract invariants: accepted and `duplicate_replay` events require `usage_event_id`, `event_contract_version`, and `source_payload_hash`; rejected events require `rejection_reason_code`; batch counts must match `event_receipts` outcomes.
- Repository SQL diagnostics now name the migration path, scan every migration for provider identifiers, and reject single-word `ALTER TABLE ... ADD COLUMN` names.
- ADR 0003 for immutable usage ingestion.
- Initial product, architecture, data-model, and accounting-boundary baseline.
- Canonical usage-event, provider-capability, and accounting-journal-proposal schemas.
- Normalized PostgreSQL billing-core migration with tenant-scoped composite references.
- Semantic journal-proposal validation for exact decimals, unique line numbers, and balanced totals.
- Meter-specific quality-rule enforcement for usage measurements.
- Offline repository-contract validation and exact-head CI.

### Security

- Excluded prompt, response, credential plaintext, provider secrets, and card data from initial contracts.
- Required commit-pinned GitHub Actions.
