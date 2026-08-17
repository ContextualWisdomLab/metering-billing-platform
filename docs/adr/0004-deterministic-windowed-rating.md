# ADR 0004: Deterministic Time-Windowed Rating Authority

**Status:** Accepted

## Context

Stored usage is an immutable commercial fact.  Buyers still need a money total for a tenant and a half-open time window before anyone drafts an invoice or talks to a payment provider.  Binary floating-point types cannot represent that money (IEEE, 2019; Cowlishaw, 2009).  Replays of the same facts must not create a second rating identity (Helland, 2012).  Windows are timezone-aware instants (International Organization for Standardization, 2019).

## Decision

- `UsageRatingService.rate_usage_window` is the rating authority for already-stored usage.
- The rating-run identity is `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)`.  The rate-card version is part of that identity because `rate_card_id` names one `(rate_card_code, rate_card_version)` row.
- The usage snapshot hashes every stored event in the window, including analytics-only and manual-review measurements, so a later quality or quantity change is a new run.
- Only measurements whose `meter_quality_rule.billing_disposition_code` is `billable` enter invoice-intent totals.  `analytics_only` and `manual_review` stay stored and stay out of the money total.
- Unit prices and line amounts are exact decimals.  A billable meter without a unit price fails closed.
- Persist append-only `rating_run` and `rating_line` rows.  A replay returns the stored `rating_run_id` and the same totals.
- Rating does not create an invoice draft, call a payment provider, or emit a posted journal.

## Consequences

- Known usage in a known window produces a known money total.
- Tenants cannot see or fund another tenant's usage.
- Invoice draft remains the next increment after this rating authority.
