# ADR 0134: Record controlled late-adjustment application

## Status

Accepted for the next implementation slice of issue #87.

## Context

ADR 0132 stores a signed immutable late-adjustment fact and ADR 0133 presents
it to an operator. The workflow needs a durable acknowledgement that the
fact was consumed, while a later rating command may still be retried or
delivered by another worker.

## Decision

Add an append-only, tenant-scoped `late_adjustment_application` fact:

- `POST /v1/late-adjustments/{late_adjustment_id}/applications` requires
  `applied_by` and `authorization_reference`;
- the application keeps the source target period, signed exact amount, and
  currency equal to the immutable late-adjustment row;
- identity is `(tenant_account_id, late_adjustment_id)`, so replay returns the
  stored application as `duplicate_replay` without a second row;
- item/list presentment changes the next action from `apply_late_adjustment`
  to `rate_late_adjustment` after the application exists;
- PostgreSQL composite foreign keys and an immutable trigger enforce tenant
  ownership, source equality, and no update/delete mutation.

This fact does not mutate a billing period, usage event, rating run, tax
assessment, journal proposal, provider state, or webhook outbox. Full
re-rating, FX treatment, statutory posting, and provider settlement remain
separate commands.

## Consequences

Workers can claim durable application progress safely across retries and
process restarts. The explicit audit references make the operator action
traceable without storing secrets or inventing statutory identifiers.

## References

PostgreSQL Global Development Group. (2026). *CREATE TRIGGER*.
https://www.postgresql.org/docs/current/sql-createtrigger.html
