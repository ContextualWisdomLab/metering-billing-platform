# ADR 0091: Operator Spend-Budget-Over Storybook

**Status:** Accepted

## Context

#101 observes a published commercial `spend_budget` and enqueues one existing #24 `spend_budget.over` outbox event on first `utilization_status=over`. #96 presents one budget evaluation. #99 presents account-level budget status. `operator_console` already has SpendBudget, BudgetStatus, and WebhookOutboxEvent stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no story for the #101 over-signal write.

This repository is not the statutory accounting authority. A commercial over observation is a control signal, not collected revenue, a reservation, or a hard-stop (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. The #101 over-signal write, GET evaluation, GET budget-status, persist, SpendBudget/RatedSpend/BudgetStatus Storybooks, and `spend_budget.published` stay unchanged.

## Decision

- Add a tokenized `SpendBudgetOver` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one same-tenant first-over accepted observation, one same-tenant under observation with zero over-signal rows, and one `duplicate_replay` of the same source. The replay HTTP body may show the current evaluation; the outbox row stays first-over-wins.
- Validate observation fixtures against the existing over-signal schema and the outbox fixture against the existing webhook-outbox presentment schema. Do not invent a parallel envelope or a second event type.
- Keep `budget_amount` and `over_amount` as exact-decimal strings. `AmountDue` presents those strings. `StatusChip` presents `utilization_status` over versus under/at. Next operator action is `wait`. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account` (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent BudgetGauge or a new money widget. `AmountDue` already presents the exact amounts.
- Do not change `POST /v1/spend-budgets/{spend_budget_id}/over-signal`. Do not add a GET side-effect. Do not change GET evaluation, GET budget-status, persist, SpendBudget/RatedSpend/BudgetStatus Storybooks, or the #24 outbox contracts.
- Do not implement atomic authorization, quotas, entitlements, reserve/commit/release, or a hard-stop. Do not invent a journal, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, evaluation snapshot persist, dimension-scoped budget, or statutory identifier.

## Consequences

- Operators can open one first-over accepted enqueue and one under observation in Storybook and see exact over/budget amounts, utilization, tenant pin, outbox growth or zero over-signal rows, and the next action: wait.
- Python remains the commercial authority. The console only presents stored #101 JSON.
- #101 stays immutable. SpendBudget, RatedSpend, and BudgetStatus Storybook stay unchanged. #85 atomic authorization remains later.
