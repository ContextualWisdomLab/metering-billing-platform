# Changelog

## [Unreleased]

### Added

- Initial product, architecture, data-model, and accounting-boundary baseline.
- Canonical usage-event, provider-capability, and accounting-journal-proposal schemas.
- Normalized PostgreSQL billing-core migration with tenant-scoped composite references.
- Semantic journal-proposal validation for exact decimals, unique line numbers, and balanced totals.
- Meter-specific quality-rule enforcement for usage measurements.
- Offline repository-contract validation and exact-head CI.

### Security

- Excluded prompt, response, credential plaintext, provider secrets, and card data from initial contracts.
- Required commit-pinned GitHub Actions.
