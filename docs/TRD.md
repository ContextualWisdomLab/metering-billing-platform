# Technical Requirements Document

## Architecture style

Start as a modular, contract-first repository. Runtime services can later be deployed as a modular monolith and split at stable ports when throughput or PCI/network boundaries justify separation.

## Contract plane

- JSON Schema Draft 2020-12 for external payloads.
- Exact decimal values represented as strings at API boundaries.
- UUIDv7 identifiers for new records where PostgreSQL 18 generates IDs.
- Idempotency keys on all state-changing external commands.
- CloudEvents-compatible envelopes in the event milestone.

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
