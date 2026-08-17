# Technical Requirements Document

## Architecture style

Start as a modular, contract-first repository. Runtime services can later be deployed as a modular monolith and split at stable ports when throughput or PCI/network boundaries justify separation.

## Contract plane

- JSON Schema Draft 2020-12 for external payloads.
- Exact decimal values represented as strings at API boundaries.
- UUIDv7 identifiers for new records where PostgreSQL 18 generates IDs.
- Idempotency keys on all state-changing external commands.
- Usage ingestion deduplicates on tenant-scoped `source_event_key` and on `source_payload_hash` plus `event_contract_version`.
- CloudEvents-compatible envelopes in the event milestone.

## Usage-ingestion plane

The importable `metering_billing` package is the first runtime module.  It can run in-process against the in-memory third-normal-form ledger that mirrors the PostgreSQL constraints.  A later adapter can persist the same rows without changing the hash, tenant, decimal, or rating-identity rules.  Canonical source-payload hashing excludes envelope identifiers (`event_id`, `source_event_key`), `source_payload_hash`, and `recorded_at`.  Batch ingest, usage queries, and rating accept half-open ISO 8601 windows.  Rating identity is tenant, window, rate-card version, and usage-snapshot hash.

## Persistence plane

PostgreSQL is the authoritative store for normalized records. Raw webhook and usage payloads will be stored immutably in S3-compatible object storage in a later milestone; relational records retain hashes and references.

## Provider plane

Provider integration is capability-based. Checkout, subscription, usage export, invoice export, collection, refund, dispute, tax document, and settlement are separate ports. A provider implements only declared capabilities.

## Accounting plane

The platform produces semantically validated, balanced journal proposals using semantic account roles and an intended book role. The Accounting Information Platform resolves authoritative chart-account IDs, accounting policy, legal entity, accounting book, fiscal period, currency treatment, revenue recognition, and final posting.

## Security

- No card number, CVC, provider secret, PAT plaintext, prompt, or response is accepted in billing contracts.
- Webhooks are evidence until signature verification and normalization succeed.
- Tenant isolation is enforced in the application and database layers.
- Historical facts are corrected by compensating records.
