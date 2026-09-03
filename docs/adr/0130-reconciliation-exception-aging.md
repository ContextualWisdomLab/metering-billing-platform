# ADR 0130: Derived reconciliation-exception aging

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Expose exception aging as a read-only projection from the immutable
`reconciliation_line.assessed_at` timestamp. The projection accepts an explicit
`as_of` instant, calculates UTC calendar days, and maps each exception to
`current`, `days_1_30`, `days_31_60`, `days_61_90`, or `days_90_plus`.

PostgreSQL reads remain tenant- and period-scoped through the existing
reconciliation-line repository. No mutable age column or second exception fact
is introduced; a later event-timestamp model can replace the source timestamp
without changing the projection boundary.

## Consequences

- Aging is reproducible for a supplied `as_of` instant and cannot rewrite a
  closed reconciliation fact.
- Resolution status and maker-checker history remain separate immutable facts.
- Exception-specific occurrence timestamps and FOCUS export remain follow-up
  work.
