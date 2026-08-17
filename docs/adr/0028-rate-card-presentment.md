# ADR 0028: Rate Card HTTP Presentment

**Status:** Accepted

## Context

#18 already publishes a tenant-scoped `rate_card` header and append-only `rate_card_version` lines.  `POST /v1/rate-cards` already applies that write.  Operators still cannot read the stored catalog as a statement with a keyset list.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).  TM Forum TMF620 treats a catalog as a versioned, queryable price list (TM Forum, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #18 store.  It must not invent a catalog shape, versioning scheme, money type, journal, or capture cards.

## Decision

- Keep `POST /v1/rate-cards` as the #18 write keyed on tenant, card name, and canonical lines.  Refuse PAN, CVC, and provider-secret fields.
- Expose `RateCardPresentmentService.present_rate_card(tenant_reference, rate_card_id)`.
- Project stored fields only: `rate_card_id`, `tenant_reference`, `rate_card_name`, `currency_code`, latest `rate_card_version` and `rate_card_version_id`, `created_at`, `published_at`, flat `lines`, and `next_operator_action` (`rate_window`).
- Expose `GET /v1/rate-cards/{rate_card_id}` as presentment.  Same-tenant hit is HTTP 200.  Cross-tenant, header-only, or unknown is HTTP 404 with no leak.
- Expose `GET /v1/rate-cards` as `{rate_cards, next_cursor}` ordered by `created_at` then `rate_card_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Keep `GET /v1/rate-cards/{rate_card_id}/versions` and `GET /v1/rate-card-versions/{rate_card_version}` as the existing #18 catalog reads.
- Do not add a presentment table, new catalog, journal, or scheduler.

## Consequences

- Operators publish a rate card, then rate a window against that version.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Rate cards remain commercial catalog facts, not books.
