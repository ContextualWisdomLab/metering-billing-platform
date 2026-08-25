# ADR 0094: Operator Spend-Budget-Approaching Storybook

**Status:** Accepted

## Context

#104 observes a published commercial `spend_budget` and enqueues one existing #24 `spend_budget.approaching` outbox event on first `utilization_status=at` the documented `budget_amount` (ADR 0082; ADR 0093). #102 presents the #101 over-signal write. `operator_console` already has SpendBudget, BudgetStatus, SpendBudgetOver, and WebhookOutboxEvent stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no story for the #104 approaching-signal write.

This repository is not the statutory accounting authority. A commercial approaching observation is a control signal, not collected revenue, a reservation, or a hard-stop (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. The #104 approaching-signal write, #101 over write, #103 over GET, GET evaluation, GET budget-status, persist, SpendBudget/RatedSpend/BudgetStatus/SpendBudgetOver Storybooks, and `spend_budget.published` stay unchanged.

## Decision

- Add a tokenized `SpendBudgetApproaching` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one same-tenant first-at accepted observation, one same-tenant under observation with zero approaching-signal rows, and one `duplicate_replay` of the same source. The replay HTTP body may show the current evaluation; the outbox row stays first-at-wins.
- Validate observation fixtures against the existing approaching-signal schema and the outbox fixture against the existing webhook-outbox presentment schema. Do not invent a parallel envelope or a second event type.
- Keep `budget_amount` and `remaining_amount` as exact-decimal strings. The first-at fixture uses `utilization_status=at` and exact `remaining_amount` `0`. `AmountDue` presents those strings. `StatusChip` presents `utilization_status` at versus under/over. Next operator action is `wait`. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account` (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent BudgetGauge or a new money widget. `AmountDue` already presents the exact amounts.
- Do not change `POST /v1/spend-budgets/{spend_budget_id}/approaching-signal`. Do not add a GET side-effect. Do not change the over write/GET, GET evaluation, GET budget-status, persist, SpendBudget/RatedSpend/BudgetStatus/SpendBudgetOver Storybooks, or the #24 outbox contracts.
- Do not implement atomic authorization, quotas, entitlements, reserve/commit/release, or a hard-stop. Do not invent a journal, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, evaluation snapshot persist, dimension-scoped budget, notify percentage, or statutory identifier.

## Consequences

- Operators can open one first-at accepted enqueue and one under observation in Storybook and see exact remaining/budget amounts, utilization, tenant pin, outbox growth or zero approaching-signal rows, and the next action: wait.
- Python remains the commercial authority. The console only presents stored #104 JSON.
- #104 stays immutable. SpendBudget, RatedSpend, BudgetStatus, and SpendBudgetOver Storybook stay unchanged. #85 atomic authorization remains later.

## References

IEEE. (2019). *IEEE standard for floating-point arithmetic* (IEEE Std 754-2019). IEEE. https://doi.org/10.1109/IEEESTD.2019.8766229

IFRS Foundation. (2024). *IFRS 15 revenue from contracts with customers*. IFRS Foundation.

PCI Security Standards Council. (2024). *Payment Card Industry Data Security Standard: Requirements and testing procedures* (Version 4.0.1). PCI Security Standards Council.
