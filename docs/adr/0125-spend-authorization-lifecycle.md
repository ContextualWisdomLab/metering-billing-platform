# ADR 0125: Atomic spend-authorization lifecycle

## Status

Accepted for the first #85 control-engine slice.

## Decision

Keep the published `spend_budget` immutable and add a tenant-scoped
authorization projection plus append-only `spend_reservation`,
`spend_commitment`, `spend_release`, and `spend_authorization_transition`
receipts. Every command carries an idempotency key, actor, purpose, policy
version, exact-decimal amount, and bounded validity deadline.

The PostgreSQL adapter locks the referenced budget row with `FOR UPDATE`
before summing prior exposure and inserting a reservation. This makes two
concurrent requests compete for the same durable hard-limit remainder. A
commit or release locks its authorization row, checks conserved exact amounts,
then inserts its receipt and updates the current projection in one transaction.
Memory remains the deterministic reference adapter.

## Consequences

The lifecycle supports `reserved`, `partially_committed`, `committed`,
`released`, `expired`, and durable `denied` outcomes. The existing budget and
rated-spend rows are not rewritten. This slice does not yet compose
credential/principal/project/cost-center/contract policies or implement
quota, credit, or entitlement ledgers; those require separate effective-dated
authorities.
