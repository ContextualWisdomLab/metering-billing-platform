# ADR 0086: Billing-Account Budget Status

**Status:** Accepted

## Context

#82 publishes and presents one append-only commercial `spend_budget`. #93 compares one published budget to already-rated spend through `GET /v1/spend-budgets/{spend_budget_id}/evaluation`. Operators still cannot inspect every published commercial budget on one same-tenant billing account in one safe read.

This repository is not the statutory accounting authority. IFRS 15 treats a commercial budget as control evidence, not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Google AIP-158 requires a deterministic keyset cursor instead of a mutable offset (Google, 2024).

No account-level budget-status read existed. This slice adds the smallest real operator surface after one-budget evaluation: project every published `spend_budget` on one same-tenant billing account using the same rated-spend plus exact remaining/over math. It does not persist, mutate budgets, hard-stop rating, ingest, or invoice draft, emit a webhook or journal, call AIS, invent a dimension-scoped budget, emit `retained_earnings` or 310100, invent a statutory identifier, or add VAT/NTS adapters.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. POST spend-budgets, one-budget evaluation GET, #24 outbox, and Storybook stay unchanged.

## Decision

- Expose `SpendBudgetEvaluationPresentmentService.list_billing_account_budget_statuses(tenant_reference, billing_account_id, cursor=None, page_limit=None)`.
- Reuse `present_spend_budget_evaluation` per published budget on that same-tenant account. Do not persist evaluation. Do not mutate budgets.
- `GET /v1/billing-accounts/{billing_account_id}/budget-status` lists `{budget_statuses, next_cursor}` ordered by `published_at` then `spend_budget_id`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100.
- Each row carries `spend_budget_id`, `currency_code`, exact `budget_amount`, `rated_amount`, `remaining_amount`, `over_amount`, `utilization_status` `under`|`at`|`over`, window instants, `spend_budget_status`, and `next_operator_action=wait`. Never mix currencies in one monetary total.
- Tenant pin matches #22. Same tenant is HTTP 200. Unknown billing account is HTTP 404. Cross-tenant account is HTTP 403. Missing `X-CWL-Tenant-Reference` (and no query `tenant_reference`) is HTTP 422. Unknown or cross-tenant budgets are omitted with no leak.
- Replay is a safe GET and writes no money fact. Float money fails closed. Exact Decimal only.
- Pin `X-CWL-Tenant-Reference` to the commercial `tenant_account` and fail closed on missing or mismatch. Do not auto-create tenants.

## Consequences

- Operators open one billing account and inspect remaining, over, and utilization for every published commercial spend budget without stopping work.
- #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.
- POST spend-budgets, `GET /v1/spend-budgets/{spend_budget_id}/evaluation`, #24 outbox, and Storybook stay unchanged.
