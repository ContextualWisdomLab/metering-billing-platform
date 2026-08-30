# ADR 0131: Reconciliation-gated period transition

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Add a PostgreSQL command that locks one tenant-owned billing period and permits
only the `soft_closed` to `reconciled` transition. It selects the latest
completed reconciliation run, requires its blocking-exception summary to equal
the persisted exception rows, requires the run membership to cover exactly all
persisted reconciliation lines for the period, and requires every exception in
that run to have at least one immutable `resolved` or `waived` resolution.

Migration 0049 repeats the lifecycle and reconciliation checks in PostgreSQL
triggers, locks the period row before checking or appending, and rejects new
reconciliation lines after reconciliation. The repository command keeps the
same checks for clear errors; no exception, run, or resolution is updated or
deleted.

## Consequences

- A period cannot become reconciled without a complete, auditable run and
  resolution evidence.
- Missing runs, inconsistent summaries, unresolved exceptions, wrong tenants,
  and non-`soft_closed` periods fail closed.
- Reconciliation calculation, late adjustments, provider settlement ingestion,
  and FOCUS export remain separate work.
