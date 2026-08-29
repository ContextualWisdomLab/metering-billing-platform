# ADR 0131: Reconciliation-gated period transition

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Add a PostgreSQL command that locks one tenant-owned billing period and permits
only the `soft_closed` to `reconciled` transition. It selects the latest
completed reconciliation run, requires its blocking-exception summary to equal
the persisted exception rows, and requires every exception in that run to have
at least one immutable `resolved` or `waived` resolution.

The checks and append-only transition use the repository's existing transaction
and period-history path. No exception, run, or resolution is updated or
deleted.

## Consequences

- A period cannot become reconciled without a complete, auditable run and
  resolution evidence.
- Missing runs, inconsistent summaries, unresolved exceptions, wrong tenants,
  and non-`soft_closed` periods fail closed.
- Reconciliation calculation, late adjustments, provider settlement ingestion,
  and FOCUS export remain separate work.
