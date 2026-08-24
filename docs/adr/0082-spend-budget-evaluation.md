# ADR 0082: Commercial Spend-Budget Evaluation

**Status:** Accepted

## Context

#82 publishes and presents one append-only commercial `spend_budget`. #77–#81 present already-rated spend for one billing account and half-open window. Operators still cannot compare one published budget to that already-rated spend. Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope.

This repository is not the statutory accounting authority. IFRS 15 treats a commercial budget as control evidence, not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024).

No evaluation read existed. This slice adds the smallest real compare: project one published `spend_budget` against already-rated spend for the same tenant, billing account, half-open window, and currency. It does not persist, mutate the budget, hard-stop rating, ingest, or invoice draft, emit a webhook or journal, call AIS, invent a dimension-scoped budget, emit `retained_earnings` or 310100, invent a statutory identifier, or add VAT/NTS adapters.

## Decision

- Expose `SpendBudgetEvaluationPresentmentService.present_spend_budget_evaluation(tenant_reference, spend_budget_id)`.
- Reuse `RatedSpendPresentmentService.present_rated_spend` with `group_by=product`. Sum only stored product rows whose `currency_code` equals the budget currency. Other currencies are omitted so the read cannot invent a mixed-currency remainder.
- `budget_amount` is the stored exact Decimal. `rated_amount` is the same-currency product sum. `remaining_amount` and `over_amount` are complementary non-negative exact Decimals. `utilization_status` is `under` when rated is below budget, `at` when equal, and `over` when rated exceeds budget. Next operator action is `wait`.
- Expose `GET /v1/spend-budgets/{spend_budget_id}/evaluation` on the existing WSGI app. Tenant pin matches #22. Same tenant is HTTP 200. Unknown or cross-tenant is HTTP 404 with no leak. Missing `X-CWL-Tenant-Reference` (and no query `tenant_reference`) is HTTP 422. Pin the header to the commercial `tenant_account` and fail closed as HTTP 403 when the stored billing account belongs to another tenant.
- Replay is a safe GET and writes no money fact. Float money fails closed. PAN, CVC, and provider secrets stay off this path.
- Do not persist an evaluation row. Do not mutate `spend_budget`. Do not hard-stop rating, ingest, or invoice draft. Do not enqueue a webhook or compose a journal.

## Consequences

- Operators publish a commercial spend budget, then inspect remaining, over, and utilization without stopping work.
- #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.
- #7, #8, #62, #75, #77–#81, and #82 stay unchanged.
