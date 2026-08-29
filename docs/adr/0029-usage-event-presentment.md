# ADR 0029: Usage Event HTTP Presentment

**Status:** Accepted

## Context

#5 already ingests canonical `usage_event` rows through `UsageIngestionService` and `POST /v1/usage-events`.  Operators still cannot read a stored event as a statement or page the tenant's usage with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #5 store.  It must not invent an ingest shape, change generated `usage_event_id` rules, call AIS, or capture cards.

## Decision

- Keep `POST /v1/usage-events` as the #5 ingest.  Refuse PAN, CVC, and provider-secret fields on the write and on each batch event.
- Expose `UsageEventPresentmentService.present_usage_event(tenant_reference, usage_event_id)`.
- Project stored fields only: `usage_event_id`, `tenant_reference`, `source_event_key`, `event_payload_hash`, event and producer contract versions, `product_code`, optional operation, attribution references, allowlisted dimensions, availability and correction lineage, `occurred_at`, `recorded_at`, measurement `meter_code` / `meter_version` / `quantity` / `unit_code` / `quality_code`, and `next_operator_action` (`rate_window`). Credentials and internal account identifiers remain absent.
- Expose `GET /v1/usage-events/{usage_event_id}` as presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Expose `GET /v1/usage-events` as `{usage_events, next_cursor}` ordered by `recorded_at` then `usage_event_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, new ingest identity, journal, or scheduler.

## Consequences

- Operators ingest usage, then rate a window against a published card.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Usage events remain commercial facts, not books.
