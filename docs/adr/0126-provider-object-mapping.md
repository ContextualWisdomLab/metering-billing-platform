# ADR 0126: Provider-sticky object mappings

## Status

Accepted for the provider-foundation slice.

## Context

Commercial facts may later be projected to an external checkout, subscription,
invoice, payment, refund, dispute, or settlement object. Retrying a command
must resolve the same provider object, and an unhealthy provider must not cause
an existing object to move silently to another provider.

## Decision

- Keep internal and provider object identifiers in an explicit, bounded,
  effective-dated `ProviderObjectMapping` contract.
- `ProviderObjectMappingRegistry.record` rejects overlapping mappings for the
  same internal object or provider object within one provider account.
- `resolve_internal` and `resolve_external` fail closed when no active mapping
  exists; they never select a fallback provider.
- A provider mapping can change only through explicit `replace`, which closes
  the old half-open interval at the replacement instant and records the new
  object while preserving the old history. A replacement with a different
  provider code is rejected.

## Consequences

Provider stickiness and replacement history are testable without a network or
provider SDK. The registry is the contract/reference layer for this slice; a
later PostgreSQL repository must preserve the same tenant/account identity,
interval, uniqueness, and explicit-replacement rules. It does not call a
provider, store credentials, or implement payment/refund/settlement commands.

Issue #86 remains open for those adapter ports and durable reconciliation.
