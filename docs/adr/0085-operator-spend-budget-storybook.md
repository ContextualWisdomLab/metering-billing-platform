# ADR 0085: Operator Spend-Budget Storybook

**Status:** Accepted

## Context

#82 publishes and presents one append-only commercial `spend_budget`. #93 compares that published budget to already-rated spend. #95 enqueues `spend_budget.published` on the existing commercial webhook outbox. `operator_console` already has invoice-draft, account-statement, and rated-window stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but it has no SpendBudget story.

This repository is not the statutory accounting authority. A commercial budget is control evidence, not collected revenue (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. #82, #93, and #95 stay unchanged as HTTP and outbox contracts.

## Decision

- Add a tokenized `SpendBudget` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one published budget under utilization, one at, and one over.
- Keep `budget_amount`, `rated_amount`, `remaining_amount`, and `over_amount` as exact-decimal strings. `utilization_status` is `under`, `at`, or `over`. Next operator action is `wait`. Float money fails closed.
- Pin fixture `tenant_reference` to the commercial `tenant_account` (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent a new money widget. A BudgetGauge is omitted because `AmountDue` already presents the exact amounts.
- Do not change `GET /v1/spend-budgets/{spend_budget_id}`, `GET /v1/spend-budgets/{spend_budget_id}/evaluation`, `POST` spend-budgets, or the #24 outbox.
- Do not implement atomic authorization, quotas, entitlements, reserve/commit/release, or a hard-stop. Do not invent a journal, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, dimension-scoped budget, or statutory identifier.

## Consequences

- Operators can open a published commercial spend budget in Storybook and see remaining, over, utilization, and the next action: wait.
- Python remains the commercial authority. The console only presents stored #82/#93 JSON.
- #82, #93, and #95 stay immutable. #85 atomic authorization remains later.
