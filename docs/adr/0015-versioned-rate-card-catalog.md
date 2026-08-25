# ADR 0015: Versioned Rate-Card Catalog

**Status:** Accepted

## Context

Windowed rating (#5 / ADR 0004) accepted a bare `rate_card_version` integer and priced usage from an in-process catalog that a buyer could not publish or freeze.  TM Forum TMF620 treats a product catalog as a versioned, queryable price list rather than an implicit runtime constant (TM Forum, 2024).  Helland (2012) requires that a replay of the same catalog command return the stored identity.  IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019).

This repository is not the statutory accounting authority.  It must not invent a hidden default price, edit a published version, apply tax or discounts, or call AIS.  Journal-proposal, credit-adjustment, and posting-receipt contracts stay unchanged.

## Decision

- Expose `RateCardService.publish_rate_card(tenant_reference, rate_card_name, currency_code, lines)`.
- Persist one tenant-scoped `rate_card` header identified by `(tenant_account_id, rate_card_name)` and one append-only `rate_card_version` whose lines live in `rate_card_line`.
- Each line carries a two-or-more-word `snake_case` `metric_code`, an exact `Decimal` `unit_amount` greater than zero, and a currency that matches the card.  Binary floats are rejected.
- Publishing the same tenant, card name, canonical line hash, and contract version returns the stored `rate_card_version` as `duplicate_replay`.  A distinct line set increments `version_number`.  A published version is never edited.
- `UsageRatingService.rate_usage_window` must resolve a persisted same-tenant `rate_card_version`.  Unknown or cross-tenant versions reject.  Rating uses the stored `unit_amount` for the matching `metric_code`.  A missing metric fails closed as `meter_price_missing` and does not invent a price.
- Expose `POST /v1/rate-cards`, `GET /v1/rate-cards`, `GET /v1/rate-cards/{rate_card_id}`, `GET /v1/rate-cards/{rate_card_id}/versions`, and `GET /v1/rate-card-versions/{rate_card_version}` on the existing WSGI app.  Tenant pin matches #15–#17.  Cross-tenant or unknown identifiers are 404.
- `POST /v1/rating-runs` still accepts `rate_card_version` as an integer.  That integer must now name a persisted same-tenant version.

## Consequences

- Operators publish a rate card, then rate a window against that version.
- A later publish freezes a new version; earlier rating runs keep the old pin.
- Tenants cannot read or rate each other's catalogs.
- Tax, discounts, and tiered or graduated prices stay out of this slice.
