# Changelog

## [Unreleased]

### Changed

- Rewrote the README as a customer and operator page. Exact-head CI, successor-PR order, and validation dumps now live in CONTRIBUTING and doctoring docs.
- Expanded ADRs 0001 and 0002 to Context / Decision / Consequences / References using only already-claimed standards.

### Added

- ADR 0003 for the invoice-intent versus revenue-recognition handoff already claimed in the PRD and architecture.
- Contributor validation runbook and doctoring validation dumps.
- Initial product, architecture, data-model, and accounting-boundary baseline.
- Canonical usage-event, provider-capability, and accounting-journal-proposal schemas.
- Normalized PostgreSQL billing-core migration with tenant-scoped composite references.
- Semantic journal-proposal validation for exact decimals, unique line numbers, and balanced totals.
- Meter-specific quality-rule enforcement for usage measurements.
- Offline repository-contract validation and exact-head CI.

### Security

- Excluded prompt, response, credential plaintext, provider secrets, and card data from initial contracts.
- Required commit-pinned GitHub Actions.
