# ADR 0135: Rate an applied late adjustment as a separate fact

- Status: accepted
- Date: 2026-08-29
- Decision owners: Billing platform

## Context

`UsageRatingService` rates stored usage snapshots against a persisted rate-card
version. `LateAdjustment` contains an immutable signed commercial delta and
opaque source evidence, not usage measurements or a rate-card reference. Making
it a synthetic `rating_run` would lose provenance and could rewrite the normal
rating lineage.

## Decision

`LateAdjustmentRatingService` consumes an existing
`late_adjustment_application` and appends one tenant-scoped
`late_adjustment_rating` fact. The fact copies the target, signed exact amount,
and currency from the application, records the rating actor and authorization,
and is replay-safe on the tenant/source identity. PostgreSQL protects the
application, source, target, amount, currency, and update/delete immutability
with migration `0051`.

The HTTP command is
`POST /v1/late-adjustments/{late_adjustment_id}/ratings`. It returns
`late_adjustment_application_not_found` until the application command has
completed. A successful rating reports `record_invoice_adjustment` as the next
action because a late adjustment is not silently converted into an ordinary
usage rating run or statutory invoice.

## Consequences

- Original usage, rating runs, periods, and late-adjustment evidence remain
  unchanged.
- Replays return the original rating fact without a second row.
- The fact records consumption of the already-authoritative commercial delta;
  recalculation from source usage and price versions remains a later workflow.
- Invoice-adjustment document composition, tax treatment, provider settlement,
  and accounting posting remain explicit downstream boundaries.

## References

- IFRS Foundation. (2024). *IFRS 15 revenue from contracts with customers*.
  https://www.ifrs.org/issued-standards/list-of-standards/ifrs-15-revenue-from-contracts-with-customers/
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP semantics* (RFC
  9110). https://www.rfc-editor.org/rfc/rfc9110
