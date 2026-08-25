# ADR 0088: Operator Budget-Status Storybook

**Status:** Accepted

## Context

#97 presents every published commercial `spend_budget` on one same-tenant billing account as `{budget_statuses, next_cursor}`. #93 evaluates one budget. #77–#81 present rated spend. `operator_console` already has SpendBudget and RatedSpend stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no account-level BudgetStatus story.

This repository is not the statutory accounting authority. A commercial budget is control evidence, not collected revenue (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). Google AIP-158 requires a deterministic keyset cursor instead of a mutable offset (Google, 2024). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. Spend-budget write/evaluation, RatedSpend Storybook, and the #24 outbox stay unchanged.

## Decision

- Add a tokenized `BudgetStatus` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one same-tenant account covering published under and over utilization, plus an `at` row because it stays cheap, and one keyset `next_cursor` page.
- Keep `budget_amount`, `rated_amount`, `remaining_amount`, and `over_amount` as exact-decimal strings. Currencies stay unmixed. `next_operator_action` stays `wait`. Render `next_cursor` as the existing keyset token, not a money widget. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account` (`urn:cwl:tenant_001`). The GET envelope stays `{budget_statuses, next_cursor}` only. Do not auto-create tenants.
- Do not invent BudgetGauge or a new money widget. `AmountDue` already presents the exact amounts.
- Do not change `GET /v1/billing-accounts/{billing_account_id}/budget-status`. Do not change spend-budget write, one-budget evaluation, RatedSpend Storybook, or the #24 outbox.
- Do not implement atomic authorization, quotas, entitlements, reserve/commit/release, or a hard-stop. Do not invent a journal, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory identifier.

## Consequences

- Operators can open published spend-budget evaluations for one billing account in Storybook and see remaining, over, utilization, the keyset cursor, and the next action: wait.
- Python remains the commercial authority. The console only presents stored #97 JSON.
- #97 stays immutable. SpendBudget and RatedSpend Storybook stay unchanged. #85 atomic authorization remains later.
