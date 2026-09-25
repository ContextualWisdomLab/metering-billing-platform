# ADR 0080: Spend-Budget Evaluation Against Rated Spend

**Status:** Accepted

## Context

#82 publishes one append-only commercial `spend_budget` for a billing account, half-open window, and currency. Rated-spend #77–#81 remain a read of exclusive stored lines. Operators still cannot tell whether that published budget is under, at, or over already-rated spend. The PRD product outcome is “control spend.”

This repository is not the statutory accounting authority. IFRS 15 treats a commercial budget as control evidence, not collected revenue (IFRS Foundation, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Helland (2012) requires that a replay of the same read return the same projection without growing the store.

No evaluation read existed. This slice adds a safe GET that reuses `RatedSpendPresentmentService` with `group_by=product`. It does not persist an evaluation row, emit a webhook or journal, hard-stop rating, ingest, or invoice draft, invent a dimension-scoped budget, emit `retained_earnings` or 310100, invent a statutory identifier, or call AIS.

## Decision

- Expose `SpendBudgetEvaluationService.evaluate_spend_budget(tenant_reference, spend_budget_id)`.
- Load the stored same-tenant `spend_budget`. Missing or cross-tenant identifiers are `spend_budget_not_found`.
- Identity is the stored budget: `spend_budget_id`, `billing_account_id`, window, `currency_code`, exact `budget_amount`, and `spend_budget_status=published`.
- Call existing `RatedSpendPresentmentService.present_rated_spend` with `group_by=product` for that account and window. Sum `rated_amount` only for rows whose `currency_code` equals the budget currency. If no matching rows exist, `rated_amount` is exact zero.
- `remaining_amount = max(0, budget_amount − rated_amount)`. `over_amount = max(0, rated_amount − budget_amount)`. Both are exact decimals.
- `utilization_status` is `under` when rated is less than budget, `at` when equal, and `over` when rated exceeds budget.
- `next_operator_action` stays `wait`. Do not invent `hard-stop` or `cut_spend`.
- Expose `GET /v1/spend-budgets/{spend_budget_id}/evaluation` on the existing WSGI app. Same tenant is HTTP 200. Cross-tenant or unknown is HTTP 404 with no leak. Missing tenant is HTTP 422.
- Do not change `POST /v1/billing-accounts/{billing_account_id}/spend-budgets` or the #82 item/list presentment fields.
- Do not add an evaluation table. Replay is a safe GET and writes no money fact.

## Consequences

- Operators publish a commercial spend budget, then inspect whether exclusive rated spend is under, at, or over that budget.
- USD rated spend does not enter a KRW budget evaluation.
- A later slice may hard-stop rating or emit a threshold webhook. This slice does not.
- #7, #8, #62, #75, and #77–#82 stay unchanged except this additive evaluation route.

## References

Fielding, R., Nottingham, M., & Reschke, J. (Eds.). (2022). *HTTP semantics* (RFC 9110). Internet Engineering Task Force. https://doi.org/10.17487/RFC9110

Helland, P. (2012). Idempotence is not a medical condition. *Communications of the ACM, 55*(5), 56–65. https://doi.org/10.1145/2160718.2160734

IFRS Foundation. (2024). *IFRS 15: Revenue from contracts with customers—Supporting material.* https://www.ifrs.org/supporting-implementation/supporting-materials-by-ifrs-standards/ifrs-15/

IFRS Foundation. (n.d.). *IAS 21 the effects of changes in foreign exchange rates.* https://www.ifrs.org/issued-standards/list-of-standards/ias-21-the-effects-of-changes-in-foreign-exchange-rates/
