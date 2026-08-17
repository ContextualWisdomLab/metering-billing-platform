# Changelog

## [Unreleased]

### Added

- Importable `metering_billing` usage-ingestion service with tenant-scoped attribution, exact-decimal measurements, and half-open ISO 8601 query windows.
- Idempotent deduplication on `(tenant, source_event_key)` and `(tenant, source_payload_hash, event_contract_version)`.
- Usage-ingestion receipt contract and PostgreSQL migration for contract version, payload-hash uniqueness, and ingestion receipts.
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
