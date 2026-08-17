# Changelog

## [Unreleased]

### Added

- Importable `metering_billing` usage-ingestion service with tenant-scoped attribution, exact-decimal measurements, and half-open ISO 8601 query windows.
- Idempotent deduplication on `(tenant, source_event_key)` and `(tenant, source_payload_hash, event_contract_version)`.
- Internal `usage_event_id` generation so a reused producer `event_id` cannot overwrite stored usage.
- Append-only `usage_ingestion_receipt` rows for every accept, replay, and reject.
- Usage-ingestion receipt contract and PostgreSQL migration for contract version, payload-hash uniqueness, and ingestion receipts.
- Receipt contract invariants: accepted and `duplicate_replay` events require `usage_event_id`, `event_contract_version`, and `source_payload_hash`; rejected events require `rejection_reason_code`; batch counts must match `event_receipts` outcomes.
- Repository SQL diagnostics now name the migration path, scan every migration for provider identifiers, and reject single-word `ALTER TABLE ... ADD COLUMN` names.
- Deterministic time-windowed rating: `UsageRatingService` produces exact invoice-intent totals from stored usage, persists append-only `rating_run` and `rating_line` rows, and replays the same `rating_run_id` for the same tenant, window, rate-card version, and usage snapshot.
- ADR 0004 for rating authority.  Rating does not draft invoices or post journals.
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
