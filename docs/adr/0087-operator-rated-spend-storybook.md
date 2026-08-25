# ADR 0087: Operator Rated-Spend Storybook

**Status:** Accepted

## Context

#77 presents already-rated spend for one billing account and half-open window, grouped by `product_code`. #78–#81 add optional `group_by=project|credential|principal|cost_center` on the same GET. `operator_console` already has invoice-draft, account-statement, rating-run, and spend-budget stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no RatedSpend story.

This repository is not the statutory accounting authority. Rated spend is presentation of stored rating and exclusive draft-line amounts, not a re-rate and not collected revenue (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. Spend-budget write, evaluation, budget-status, and the #24 outbox stay unchanged.

## Decision

- Add a tokenized `RatedSpend` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one product-grouped morning window (`group_by` omitted or `product`) and one project-grouped window.
- Keep `rated_amount` as an exact-decimal string. Currencies stay unmixed. The existing presentment does not publish `next_operator_action`; operator copy stays “Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice.” Float money fails closed.
- Pin fixture `tenant_reference` to the commercial `tenant_account` (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent a new money widget. `AmountDue` already presents the exact amounts.
- Do not change `GET /v1/billing-accounts/{billing_account_id}/rated-spend`. Do not change spend-budget write, evaluation, budget-status, or the #24 outbox.
- Do not implement atomic authorization, quotas, entitlements, reserve/commit/release, or a hard-stop. Do not invent a journal, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory identifier.

## Consequences

- Operators can open already-rated product and project spend in Storybook and see exact amounts plus the next action: draft an invoice.
- Python remains the commercial authority. The console only presents stored #77–#81 JSON.
- #77–#81 stay immutable. SpendBudget Storybook stays unchanged. #85 atomic authorization remains later.
