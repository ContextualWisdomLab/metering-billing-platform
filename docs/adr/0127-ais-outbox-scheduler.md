# ADR 0127: Explicit AIS Outbox Scheduler Loop

**Status:** Accepted

## Context

The AIS posting-receipt outbox already has a tenant-scoped, idempotent drain
and an HTTP operator trigger. A production worker still needs a repeatable
cycle for multiple tenants, dynamic tenant onboarding, and prompt shutdown.
The worker must not introduce a second delivery state machine or turn a
provider observation into a second commercial effect.

## Decision

- `AisOutboxScheduler.run_once()` resolves the configured tenant source and
  delegates every tenant to `AisOutboxDrainService.drain_ais_outbox`.
- `run_forever()` repeats at a positive configured interval and waits on a
  cooperative `threading.Event`; setting that event wakes the worker without
  waiting for the full interval.
- `run_once()` returns one-cycle `(tenant_reference, drain_result)` pairs, and
  `run_forever()` forwards them to an optional `on_cycle` observer for operator
  metrics or receipts. It does not log payloads, credentials, or provider
  response bodies.
- Network, tenant, envelope, and provider failure semantics remain the drain
  service's existing fail-closed result codes. Unexpected programming errors
  are allowed to stop the worker so the deployment supervisor can restart it.

## Consequences

The same replay-safe drain can run from an operator command or a supervised
worker. Tenant discovery is injected, so a PostgreSQL deployment can refresh
its source without changing the commercial drain contract. A worker still
needs deployment-specific credentials, tenant discovery, metrics, and a
supervisor; this library primitive does not claim those integrations exist.
