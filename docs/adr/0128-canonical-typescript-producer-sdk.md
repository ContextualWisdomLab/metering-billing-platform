# ADR 0128: Canonical TypeScript producer SDK reference

## Context

Issue #90 requires heterogeneous CWL products to emit one provider-neutral
usage contract. The Python reference and conformance vector in ADR 0125 are
the compatibility authority; producer applications must not implement their
own pricing or billing logic.

## Decision

Publish a small TypeScript reference package under sdks/typescript that:

- exposes a closed typed usage event and CloudEvents 1.0 envelope;
- validates identifiers, quality, timestamps, and exact-decimal quantity
  strings before construction;
- canonicalizes the source payload with stable field ordering, normalized UTC
  timestamps, and normalized decimal text;
- computes the same SHA-256 source-payload hash as the Python reference; and
- verifies the committed cross-language conformance vector in Node's built-in
  test runner.

The package performs no price calculation or credential persistence. Its
`FileUsageOutbox` provides process-local durable buffering and its optional
`httpUsageIngestionTransport` sends batches only to HTTPS endpoints with a
bounded timeout. Applications still own endpoint selection, credential values,
and scheduling; the server remains the ingestion and monetary-effect
authority.

## Consequences

TypeScript producer integrations can share the Python reference's hash and
contract without copying private content into usage events. Real producer
smoke and replay evidence remain open under issue #90.
