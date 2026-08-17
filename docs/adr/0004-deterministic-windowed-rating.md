# ADR 0004: Deterministic Time-Windowed Rating

**Status:** Accepted

## Context

Buyers already persist immutable usage through `UsageIngestionService`.  The next commercial fact is invoice-intent money: a known tenant window of stored usage must produce a known exact total.  IEEE (2019) and Cowlishaw (2009) forbid binary floating-point types at that boundary.  ISO 8601-1:2019 already defines the half-open window used for ingest and query.  Helland (2012) requires that a replay of the same command return the same stored identity rather than a second monetary effect.

Rating is still not statutory accounting.  It must not post a journal, talk to a payment provider, or draft an invoice.

## Decision

- Expose `UsageRatingService.rate_usage_window` for one tenant, one half-open ISO 8601 window, and one rate-card version.
- Identify a rating run by `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)`.  `rate_card_id` names one `(rate_card_code, rate_card_version)` row.
- Normalize window bounds and usage instants to UTC so `Z` and `+00:00` are the same window.
- Include every stored measurement in the usage snapshot.  Equivalent decimal spellings hash as one quantity.
- Rate only measurements whose `meter_quality_rule.billing_disposition_code` is `billable`.  `analytics_only` and `manual_review` remain stored and stay out of invoice-intent totals.
- Persist append-only `rating_run` and `rating_line` rows.  An identical replay returns the same `rating_run_id` and exact totals.
- Reject a billable meter that has no unit price.  Reject an unrecognized billing disposition.  Reject binary floating-point prices at the rate-card boundary.
- Do not write an invoice draft, payment-provider command, or posted journal.

## Consequences

- Known usage in a known window reproduces one exact money total.
- Tenants cannot see or price each other's usage.
- A later invoice-draft increment can explain every line from a persisted rating run.
- Usage correction, reversal, and payment-provider adapters remain subsequent increments.
