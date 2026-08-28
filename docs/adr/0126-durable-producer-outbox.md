# ADR 0126: Durable local producer outbox

- Status: Accepted
- Date: 2026-08-28
- Owners: Metering Billing Platform

## Context

Issue #90 requires producer SDKs to survive temporary Billing unavailability
without losing a usage fact, while preserving at-least-once transport and
preventing a retry from creating a second monetary effect. The existing
canonical Python builder validates a usage event, but callers still need to
choose between dropping that event and writing an ad-hoc queue. Producer
applications also rotate credentials independently of queued commercial facts.

## Decision

`ProducerOutbox` provides the smallest durable local boundary needed by a
producer integration:

- SQLite stores the validated event JSON, tenant-scoped `source_event_key`,
  purpose, correlation ID, credential reference, attempt metadata, and delivery
  state. It never stores the bearer credential or event content outside the
  closed usage-event contract.
- The event ID is the stable local outbox ID. Re-enqueueing the same tenant,
  source key, and byte-stable event is a duplicate enqueue; reusing the key for
  a different fact fails closed.
- A tenant- and context-scoped lease prevents two local workers from delivering
  the same row at once. A drain claims only rows matching its tenant, purpose,
  credential reference, and correlation context. Claiming records the lease but
  does not consume an attempt; the attempt count advances only when the result
  is applied. An expired lease is eligible for recovery after a process crash,
  and a late result can update a row only while its original lease is still
  current.
- Each drain is capped at 100 events. `accepted` and `duplicate_replay` are
  terminal delivery outcomes; explicit `rejected` results enter dead-letter
  state; transport errors and incomplete receipts retry with capped exponential
  backoff until the configured attempt limit.
- The transport receives tenant and purpose context plus an ephemeral current
  credential. Billing remains responsible for tenant-scoped idempotency and
  at-most-once monetary effect; the outbox does not calculate price, tax,
  credit, or invoice amounts.
- SQLite write transactions use a bounded busy timeout and a per-connection
  lock; transport I/O runs outside that lock so local enqueue is not held behind
  a slow network call.

The transport is a protocol rather than an HTTP client. This keeps endpoint
policy, authentication, and language-specific networking in the producer
integration while giving Python, Rust, and TypeScript implementations one
state-machine contract to reproduce.

## Consequences

The Python SDK can buffer and replay events across process restarts with local
stdlib dependencies only. A producer must supply a durable filesystem path,
keep the current credential outside the outbox, and expose the same stable
source key on every retry. The outbox is local durability, not a replacement
for PostgreSQL Billing persistence or a cross-process queue; those remain in
issue #84 and the producer deployment boundary.

## References (APA 7)

Cloud Native Computing Foundation. (2024). *CloudEvents specification* (Version
1.0.2). https://github.com/cloudevents/spec

ContextualWisdomLab. (2026). *Publish canonical SDKs and onboard three
heterogeneous CWL usage producers* (Issue #90). GitHub.
