# ADR 0079: Commercial Spend-Budget Publish

**Status:** Accepted

## Context

#77–#81 present already-rated spend for one billing account and half-open window. Those reads stay immutable. The PRD product outcome is still “control spend.” DATA_MODEL future extensions name spend reservations; a published commercial budget is not a reservation and is not a comparison to rated spend.

This repository is not the statutory accounting authority. IFRS 15 treats a commercial budget as control evidence, not collected revenue (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same identity return the stored fact. RFC 9110 treats GET as a safe, idempotent read and maps an accepted or replayed write to 200 (Fielding et al., 2022). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Google AIP-158 requires a deterministic keyset cursor instead of a mutable offset (Google, 2024).

No spend-budget write existed. This slice adds the smallest real command: persist one append-only commercial `spend_budget` for one same-tenant billing account, window, and currency. It does not compare the budget to rated spend, hard-stop rating, ingest, or invoice draft, emit a webhook or journal, call AIS, invent product/project/credential/principal/cost-center scoped budgets, emit `retained_earnings` or 310100, or invent a statutory identifier.

## Decision

- Expose `SpendBudgetService.publish_spend_budget(tenant_reference, billing_account_id, currency_code, budget_amount, time_window, source_payload_hash=None)`.
- Persist one append-only `spend_budget`. Internal identity is opaque generated `spend_budget_id`. Natural identity is `(tenant_account_id, billing_account_id, window_started_at, window_ended_at, currency_code, source_payload_hash, spend_budget_contract_version)`.
- Accept an ISO 4217 `currency_code` and an exact `Decimal` `budget_amount` greater than zero. The window is the same half-open ISO 8601 `TimeWindow` as rated-spend #77.
- Compute `source_payload_hash` from the canonical payload (`billing_account_id`, `currency_code`, exact `budget_amount`, UTC window instants, contract version). If the caller sends a hash, it must match or the write fails closed as `request_invalid`.
- Replay of the same identity returns the stored `spend_budget_id` as `duplicate_replay` and does not grow the store. A later distinct amount or hash for the same account+window+currency appends a new row. A published budget is never mutated.
- Status is `published` only. `spend_budget_id` is not a statutory number.
- Expose `POST /v1/billing-accounts/{billing_account_id}/spend-budgets` on the existing WSGI app. Tenant pin matches #22. Missing tenant is HTTP 422. Unknown billing account is HTTP 404. Cross-tenant account is HTTP 403. PAN, CVC, and provider secrets are refused.
- Expose `SpendBudgetPresentmentService.present_spend_budget` and `list_spend_budgets`. `GET /v1/spend-budgets/{spend_budget_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants or unknown identifiers, with no leak. `GET /v1/spend-budgets` lists `{spend_budgets, next_cursor}` ordered by `published_at` then `spend_budget_id`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100.
- Next operator action is `wait`. Do not compare rated spend. Do not compose a journal. Do not enqueue `spend_budget.published`.

## Consequences

- Operators publish a commercial spend budget for one account window, then wait. Later slices may compare that budget to rated spend.
- USD and KRW in the same window are different rows because currency is part of identity.
- Tenants cannot read or publish each other's budgets.
- #7, #8, #62, #75, and #77–#81 stay unchanged.
