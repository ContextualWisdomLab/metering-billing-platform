# Changelog

## [Unreleased]

### Added

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
