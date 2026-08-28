# ADR 0125: Canonical producer SDK reference

## Context

Issue #90 needs heterogeneous CWL products to emit the same billable usage
fact. The repository already owns the closed `usage-event` schema, exact
decimal quantities, tenant-scoped idempotency, and the source-payload hash.
Duplicating that logic in each producer would allow the same fact to hash or
validate differently.

## Decision

Add a dependency-free Python reference builder that:

- exposes only the fields in the published usage contract, including the
  versioned allowlist for non-sensitive producer dimensions;
- computes `source_payload_hash` from the shared canonical representation;
- rejects schema-invalid quantities, unknown dimensions, and sensitive text;
- wraps the validated data in a CloudEvents 1.0 JSON envelope; and
- publishes one checked-in conformance vector containing the canonical JSON
  bytes and expected SHA-256 digest.

The hash covers the usage data, not CloudEvents transport metadata. The server
`UsageIngestionService` remains responsible for tenant resolution,
deduplication, durable storage, and ingestion receipts.

The canonical timestamp rule is part of this handoff contract: `occurred_at`
must be offset-aware, is normalized to UTC, and uses the reference
implementation's `datetime.isoformat()` precision (no fractional component for
zero microseconds, otherwise six digits) before the UTC offset is rendered as
`Z`. For example, `.004Z` becomes `.004000Z` in the canonical JSON.

## Consequences

Rust and TypeScript implementations can target the same vector before they are
published. Durable outbox, retry, and dead-letter behavior is documented in
ADR 0129; tracing extensions, released SDK pins, and the three real producer
integrations remain follow-up work under issue #90.
