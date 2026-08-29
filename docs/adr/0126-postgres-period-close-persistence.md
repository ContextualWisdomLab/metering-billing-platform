# ADR 0126: Durable period-close facts

## Status

Accepted for the second implementation slice of issue #87.

## Decision

Persist the immutable period-close foundation in PostgreSQL using normalized
tables for billing periods, transitions, FX rates, FX conversions,
reconciliation lines, and reconciliation exceptions. A period's current state
is derived from its transition rows; saving a later immutable snapshot appends
only new transitions and never updates prior facts. Tenant ownership is
resolved through `tenant_account_id`, and reconciliation lines inherit their
tenant from the referenced period. FX conversions retain the copied rate and
must match the referenced rate snapshot before insertion.

The repository uses one transaction for each aggregate write, idempotent
opaque identifiers, composite tenant foreign keys, exact PostgreSQL `numeric`
values, and database checks for currency, status, arithmetic, and rounding
contracts. Maker-checker authorization, late adjustments, FOCUS 1.4 export,
provider settlement ingestion, and HTTP presentment remain later #87 slices.

## Consequences

- Restarting the process does not lose period state, rate evidence, conversion
  results, or reconciliation exceptions.
- Replaying an identical fact returns the stored snapshot; changing an
  existing identifier fails closed instead of rewriting history.
- The schema is durable and tenant-scoped, but it does not yet provide a
  maker-checker workflow or a period-wide reconciliation command.
