# ADR 0003: Immutable Usage Ingestion With Hash-Version Deduplication

**Status:** Accepted

## Context

Buyers emit usage at least once.  A replay must not create a second stored fact, and a mutated payload behind the same source key must not silently replace history.  Time, money, and delivery standards already constrain the interchange format (ISO 8601-1:2019; IEEE, 2019; Cowlishaw, 2009; Fielding et al., 2022; Helland, 2012).

## Decision

- Persist usage events as append-only rows keyed by `(tenant_account_id, source_event_key)`.
- Generate the internal `usage_event_id`.  The producer `event_id` is stored as `producer_event_id` and is unique per tenant.
- Treat `(tenant_account_id, source_payload_hash, event_contract_version)` as a second identity.  An identical replay is acknowledged; a different hash or contract version is a conflict.  Equivalent decimal and UTC instant spellings hash to the same digest.
- Keep attribution tenant-scoped.  A billing account, principal, or credential URN from another tenant is rejected.
- Store quantities as exact decimals.  Binary floating-point values are not accepted.
- Query and optional batch bounds use half-open ISO 8601 windows `[window_started_at, window_ended_at)`.
- Do not emit a posted journal from ingestion.  Accounting remains a later proposal-only export.

## Consequences

- Known event batches reproduce the same stored usage set.
- Producers can retry safely after timeouts.
- Cross-tenant attribution cannot create a billable fact.
- Rating, invoice intent, and payment-provider adapters remain subsequent increments.
