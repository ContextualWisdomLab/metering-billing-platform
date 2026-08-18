# ADR 0050: Collection Aging Presentment

**Status:** Accepted

## Context

#26 lets operators list collection cases, but buyers have no current / 1-30 / 31-60 / 61-90 / 90+ outstanding view. Remaining lives on `collection_case`. Written-off leftover is already exact zero. Settled cases are no longer collectible. Drafts have no due terms; an issued invoice may store optional `due_at`.

This repository is not the statutory accounting authority. Aging is presentation of remaining consideration, not a posted AR trial balance (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum.

No aging presentment existed. This slice adds a read-only projection. It does not invent a journal, webhook, write-off, settlement, payment, dunning engine, AIS call, statutory numbering, or new money fact.

## Decision

- Expose `CollectionAgingPresentmentService.present_collection_aging(tenant_reference)`.
- Expose `GET /v1/collection-aging`. Tenant pin matches #22. Missing tenant is HTTP 422.
- Age only stored `open` or `dunning` cases whose remaining outstanding is a positive exact decimal. Settled cases and exact-zero remaining are omitted.
- Bucket by days past due from `as_of` date: `current` (not yet due or due today), `days_1_30`, `days_31_60`, `days_61_90`, `days_90_plus`.
- Due date is issued-invoice `due_at` for the same draft when stored; otherwise `collection_case.opened_at`.
- Group totals by `currency_code`. Each currency row carries case count and exact inclusive outstanding for every bucket. Do not mix currencies.
- Do not return case-id lists. Do not add a presentment table.

## Consequences

- Operators open the aging statement, then collect or credit from the existing case list.
- Storybook can consume this JSON. This slice does not add a production SPA.
- #10, #26, #45, #46, #49, #51, and #52 stay unchanged.
