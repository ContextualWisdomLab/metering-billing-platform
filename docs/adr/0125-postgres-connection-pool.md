# ADR 0125: Bounded PostgreSQL Connection Pool

**Status:** Accepted

## Context

The Compose HTTP tier is multithreaded, but a durable ledger created through
`PostgresUsageLedger.connect` previously shared one psycopg session behind a
process-wide lock.  That preserved transaction safety while serializing
unrelated requests and coupling readiness latency to tenant reads.  The
existing injected-connection form is still needed by tests and callers that
own a session lifecycle.

## Decision

- `PostgresUsageLedger.connect` uses a dependency-free, queue-backed pool with
  a hard maximum of four sessions by default.  Operators may set
  `METERING_BILLING_POSTGRES_POOL_SIZE` or pass a positive `pool_size`; invalid
  values fail closed at construction.
- Sessions open lazily, return to the idle queue after successful work, and
  are retired after psycopg operational errors or when they report closed or
  broken.  Checkout waits in bounded polling intervals when all slots are
  leased; it never opens an unbounded number of sessions.
- An outer `transaction()` or `ingestion_transaction()` holds one lease in
  thread-local state so nested repository calls share the same transaction.
  Unwrapped operations do not promise session affinity; callers requiring
  transaction-local state must use one of these contexts.
  The historical `PostgresUsageLedger(connection)` form remains a one-session
  pool and does not close the injected connection unless explicitly owned.
- `close()` stops new leases and closes idle sessions created by the ledger.
  In-flight leases finish through their existing context managers; shutdown
  behavior remains bounded by the HTTP/Compose lifecycle contract.

## Consequences

- Concurrent HTTP requests can use independent database sessions without
  changing service or SQL contracts.  The pool size is bounded and visible to
  operators, while startup still fails closed for invalid configuration.
- The first Compose load result remains a historical single-session baseline.
  It must be rerun against the merged deployment before making capacity or
  latency claims; this ADR records no performance improvement by itself.
- No schema, payload, secret, provider, or retry contract changes.  Rollback
  is the normal protected deployment rollback to the prior application image,
  which returns to the single-session `connect` behavior.

## References

- ADR 0123: PostgreSQL Ledger Backend Selection for the HTTP App.
- ADR 0124: Compose Deployment Surface and k6 Load Baseline.
