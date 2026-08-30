# ADR 0129: Immutable reconciliation run envelope

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Persist a completed `reconciliation_run` as an immutable summary linked to an
ordered, normalized set of reconciliation lines. The run records its period,
start and completion instants, and blocking-exception count; the child table
records line membership rather than embedding line objects in the run row.

The repository accepts the completed run facts but does not calculate them,
resolve exceptions, or advance the billing-period lifecycle in this slice. A
later command can build this envelope from provider, internal, cash, and
resolution evidence. Late-adjustment recording is separate migration `0049`
(ADR 0132); this run does not apply or re-rate those facts.

## Consequences

- Reconciliation runs have durable replay and line-level drill-down identity.
- A run cannot include a line from another tenant or period.
- PostgreSQL rejects updates and deletes for completed runs and their line
  membership; later immutability changes use forward-only migrations so an
  already-applied migration checksum remains stable.
- Run calculation, completeness gates, aging, and period reconciliation remain follow-up work.
