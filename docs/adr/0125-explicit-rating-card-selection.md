# ADR 0125: Explicit Rating-Card Selection

**Status:** Proposed
**Date:** 2026-10-03

## Context

PRD windowed-rating acceptance requires published same-tenant pricing and forbids
hidden prices. Gap issues #84 and #89 require durable behavior and explainable
pricing authority. Several cards can independently publish version 1.

The service previously treated a missing explicit card as a preference and
looked up any unique version in the tenant. The HTTP adapter also ignored the
request's `rate_card_code`. A premium-card request could therefore silently use
standard pricing, including replay of an existing standard run.

Existing open PRs own PostgreSQL client replacement (#180 / issue #176), spend
authorization (#143), producer SDKs (#146), and broader finance integration.
This repair does not duplicate or claim completion of those workstreams.

## Decision

- `POST /v1/rating-runs` accepts optional `rate_card_code`, the exact published
  `rate_card_name`, alongside the existing integer `rate_card_version`.
- Forward the selector to `UsageRatingService.rate_usage_window`.
- An explicit selector must resolve that same tenant, card name, and version.
  Missing or foreign-only names return `rate_card_not_found`, with no rating
  insert and no differently priced replay. Non-string/empty service selectors
  also fail closed without reaching adapter-specific dictionary or SQL errors.
- Explicit HTTP null, non-string, empty, and whitespace-only selectors return
  HTTP 422 `request_invalid`; omission remains distinct from malformed input.
- Preserve the legacy omitted-selector behavior on the default service:
  prefer a stored `cwl_standard` version; if absent, accept only a unique
  same-tenant version. Never choose arbitrarily among nondefault cards.
- A configured nondefault service card has binding authority and cannot fall
  back when absent. A per-call selector can explicitly choose another card.
- Preserve rating identity, exact-decimal arithmetic, frozen historical runs,
  append-only invoice drafts, response contracts, and existing tenant pin/API
  credential rules. No migration, provider call, journal post, or new dependency.

## Verification

Regression tests first reproduce explicit-name substitution, ignored HTTP
selection, malformed-selector failures, and configured-card substitution.
Tests cover the positive usage-to-rating-to-draft path with two cards sharing
version 1, idempotent replay, unknown and foreign-only names, legacy omission,
and ambiguity rejection. PostgreSQL tests reopen a real database connection,
replay the selected run, and read the same exact draft amount.

The repository's full production statement/branch coverage gate remains 100%.
These tests prove this bounded pricing repair, not GA readiness or production
identity/provider integration.

## Consequences

Callers with multiple price books can select a card deterministically. Existing
calls without a selector retain compatibility. Calls that relied on silently
substituting an explicit absent card now reject intentionally. Previously
stored runs and invoices are not rewritten; monetary corrections remain
explicit domain records. Proposed status remains until protected integration.
