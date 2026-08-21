# ADR 0080: Durable PostgreSQL Usage Ingestion

**Status:** Accepted

## Context

The usage contract, tenant attribution, exact-decimal measurements, and
idempotent receipts were previously exercised through `MemoryUsageLedger`.
The PostgreSQL migrations already defined the normalized tables and identity
constraints, but no runtime write path used them. That gap allowed a green
in-process test to be mistaken for restart-safe durable ingest.

PostgreSQL 18 documents `ON CONFLICT DO NOTHING` as the alternative to a
unique or exclusion violation and guarantees that the insert either proceeds
or takes the conflict action for each row. The repository therefore needs to
let the database arbitrate concurrent writes, then classify the existing
tenant-scoped fact. [PostgreSQL INSERT documentation](https://www.postgresql.org/docs/18/sql-insert.html)
and [PostgreSQL constraint documentation](https://www.postgresql.org/docs/18/ddl-constraints.html)
also support keeping identity and interval rules in the database. Helland
(2012) requires replay to acknowledge the stored fact rather than mutate it.

## Decision

- Add `PostgresUsageLedger`, a psycopg 3 repository for tenant, account,
  principal, credential, assignment, meter, quality, usage-event,
  measurement, and ingestion-receipt rows.
- Keep the connection injectable for pools and tests; `connect()` is the
  standalone entry point and never creates an in-memory fallback.
- Use one outer PostgreSQL transaction for the ingest decision, normalized
  event/measurement insert, and audit receipt. A failed measurement insert
  rolls back its parent event.
- Use database uniqueness for concurrent deduplication. Exact replay returns
  the stored event; changed source, payload, or producer identity produces the
  existing rejection reason and still records a receipt.
- Resolve effective principals, credential assignments, and meter versions by
  PostgreSQL half-open time predicates. Tenant references and composite foreign
  keys remain the isolation boundary.
- Run the adapter against PostgreSQL 18 in CI with the checked-in migrations,
  and keep the runtime dependency export hash locked.

## Consequences

The first durable vertical slice now has observed restart-safe, rollback-safe,
tenant-scoped, and concurrent PostgreSQL evidence. The broader invoice,
collection, provider, accounting-outbox, raw-payload object storage,
deployment readiness, migration recovery, backup/restore, performance, and
OpenTelemetry requirements remain open under issue #84; this ADR does not
claim that the entire commercial platform is durable or GA.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). *INSERT*.
- PostgreSQL Global Development Group. (2026). *Constraints*.
