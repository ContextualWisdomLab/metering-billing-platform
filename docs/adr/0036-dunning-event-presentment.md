# ADR 0036: Dunning Event HTTP Presentment

**Status:** Accepted

## Context

#10 already records append-only `collection_dunning_event` rows through `POST /v1/collection-cases/{collection_case_id}/dunning-events`.  #26 collection-case presentment exposes only last/next notice summaries plus a nested `dunning_events` array on the case.  Operators still cannot GET one stored reminder or page tenant-wide notice history.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

HTTP must expose the existing `collection_dunning_event` store.  It must not invent a send, email, SMS, or PSP engine.

## Decision

- Keep `POST /v1/collection-cases/{collection_case_id}/dunning-events` as the #10 record command.  Refuse PAN, CVC, and provider-secret fields on that write.  Do not add a delivery command.
- Expose `DunningEventPresentmentService.present_dunning_event(tenant_reference, dunning_event_id)`.
- `GET /v1/dunning-events/{dunning_event_id}` returns the tenant-scoped presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `dunning_event_not_found`.
- Project stored facts only: `dunning_event_id` (the stored `collection_dunning_event_id`), `tenant_reference`, `collection_case_id`, `dunning_event_number`, `dunning_notice_code`, `occurred_at`, and `next_operator_action` (`wait` when the parent case is settled, otherwise `collect`).
- Expose `GET /v1/dunning-events` as `{dunning_events, next_cursor}` ordered by `occurred_at` then `collection_dunning_event_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Never return recipient PII, channel, provider id, delivery status, notice body, or an invented notice amount.

## Consequences

- Operators record the commercial reminder, then collect or credit.
- Collection case status and settlement stay the #10 rules.  There is no automatic communication.
- Storybook can consume this JSON.  This slice does not add a production SPA.
