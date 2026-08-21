# ADR 0081: Durable PostgreSQL Usage-to-Invoice Vertical Slice

**Status:** Accepted

## Context

`PostgresUsageLedger` previously stopped at immutable usage ingestion. Rate
cards, rating runs, invoice drafts, issued invoices, and the commercial
webhook outbox still had only the `MemoryUsageLedger` path. A successful
in-process request therefore did not prove that a restart preserved the
buyer-visible commercial fact.

## Decision

- Extend the PostgreSQL repository through tenant-scoped rate-card versions,
  rating runs, invoice drafts, issued-invoice snapshots, optional tax reads,
  and `invoice.issued` outbox events.
- Keep immutable header and line rows in PostgreSQL with exact `numeric` values,
  composite tenant foreign keys, append-only identities, and migration 0038's
  upgrade backfills for canonical billing-account and meter references.
- Wrap publish, rate, draft, and issue commands in the repository transaction
  boundary. Issued-invoice insertion and outbox enqueue commit together;
  replay returns the stored rows without adding a second event.
- Keep `MemoryUsageLedger` as the fast reference implementation. This slice is
  not a claim that the HTTP default, collection/payment/provider lifecycle,
  raw object storage, RLS policy, readiness, backup/restore, HA, capacity
  benchmark, or OpenTelemetry requirements are complete.

## Evidence

The dedicated PostgreSQL 18 integration suite exercises clean migration,
tenant isolation, exact decimal totals, tax snapshots, replay, direct unique
conflicts, rollback, and concurrent usage ingestion. The repository suite
passes 601 tests with 100% statement and branch coverage.

## Consequences

The first durable buyer-visible path is now observable as:

```text
usage event -> rate-card version -> rating run -> invoice draft
           -> issued invoice + invoice.issued outbox event
```

Issue #84 remains open for the other authoritative commercial records and
production recovery/deployment controls.

## References

- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
