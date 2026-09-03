# ADR 0127: Maker-checker reconciliation resolutions

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Persist each disposition of a typed reconciliation exception as a separate
immutable `reconciliation_resolution` fact. A resolution records its owner,
reason, evidence reference, resolved or waived status, and distinct maker and
checker references. It can reference only an exception already stored on the
same reconciliation line. Replaying an opaque resolution identifier returns
the original fact; changing it fails closed.

This slice records authorization evidence but does not rewrite the original
reconciliation line or declare a period reconciled. A later reconciliation-run
command will evaluate all blocking exceptions and their resolutions together.

## Consequences

- Material exception dispositions have durable, queryable maker-checker history.
- Provider and internal amounts remain immutable while an operator explains a
  resolution or waiver.
- Run-level completeness, aging, assignment queues, late adjustments, and
  period transition authorization remain follow-up work.
