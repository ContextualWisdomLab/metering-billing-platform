# ADR 0030: Rating Run HTTP Presentment

**Status:** Accepted

## Context

#7 already rates a tenant window through `UsageRatingService.rate_usage_window` and `POST /v1/rating-runs`.  Operators still cannot read a stored `rating_run` as a statement or page the tenant's rated windows with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #7 store.  It must not invent a rating shape, change rating identity, call AIS, or capture cards.

## Decision

- Keep `POST /v1/rating-runs` as the #7 rate-a-window command.  Refuse PAN, CVC, and provider-secret fields on the write.
- Expose `RatingRunPresentmentService.present_rating_run(tenant_reference, rating_run_id)`.
- Project stored fields only: `rating_run_id`, `tenant_reference`, `rate_card_code`, `rate_card_version`, `window_started_at`, `window_ended_at`, `usage_snapshot_hash`, `currency_code`, `rated_total_amount`, `recorded_at`, rating lines, and `next_operator_action` (`draft_invoice`).
- Expose `GET /v1/rating-runs/{rating_run_id}` as presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Expose `GET /v1/rating-runs` as `{rating_runs, next_cursor}` ordered by `recorded_at` then `rating_run_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, new rating engine, journal, or status field.

## Consequences

- Operators rate a window, then draft an invoice.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Rating runs remain commercial facts, not books.
