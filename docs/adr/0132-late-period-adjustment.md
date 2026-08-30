# ADR 0132: Closed-period late adjustment fact

## Status

Accepted for the next implementation slice of issue #87.

## Context

Soft close freezes the ordinary usage and rating snapshot, while a hard-closed
period must never be rewritten. A late provider event, correction, or reversal
therefore needs a durable fact that points from the closed source period to a
later period. This repository is not the statutory accounting authority; the
fact is commercial evidence and does not post a journal, create a legal tax
document, or restate the source period.

## Decision

Add an immutable `late_adjustment` fact with:

- opaque `late_adjustment_id` and tenant-scoped source and target period keys;
- closed `adjustment_kind` values `late_usage`, `correction`, and `reversal`;
- signed exact `adjustment_amount` plus an ISO 4217-shaped currency code;
- non-PII `source_reference`, SHA-256 payload identity, and `recorded_at`.

The source period must be at least `soft_closed`, the target period must be
`open`, and `target_period.start >= source_period.end`. The original usage,
rating, reconciliation, and period rows are never updated. The stable replay
identity is `(tenant, source_reference)`; the payload identity is
`(tenant, source_period, target_period, adjustment_kind, source_payload_hash,
contract_version)`. An identical retry, including one with a regenerated
opaque ID, returns the stored fact; a changed payload is a conflict.

The Python contract validates exact money and PostgreSQL migrations `0049`/`0050`
enforce tenant-scoped foreign keys, lifecycle/order checks, the idempotency key, and
UPDATE/DELETE rejection with row triggers. PostgreSQL documents that a
`BEFORE` row trigger runs before the row operation and may reject it (PostgreSQL
Global Development Group, 2026).

## Consequences

- Late usage and correction evidence survives process restart without changing
  the closed-period snapshot.
- The later-period rating/journal application remains a separate follow-up;
  this slice records the source fact only.
- Currency is an uppercase three-letter boundary consistent with ISO 4217,
  but no FX conversion is inferred here.
- Reconciliation calculation, provider settlement ingestion, FOCUS export,
  statutory posting, and customer-facing workflow remain separate slices.

## References

International Organization for Standardization. (2015). *Codes for the
representation of currencies* (ISO Standard No. 4217).
https://www.iso.org/standard/64758.html

IFRS Foundation. (2024). *IFRS 15: Revenue from contracts with customers*.
https://www.ifrs.org/issued-standards/list-of-standards/ifrs-15-revenue-from-contracts-with-customers/

PostgreSQL Global Development Group. (2026). *CREATE TRIGGER*.
https://www.postgresql.org/docs/current/sql-createtrigger.html
