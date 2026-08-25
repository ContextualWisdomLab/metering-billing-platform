# ADR 0105: Operator Leftover / Unapplied-Cash Storybook

**Status:** Accepted

## Context

#54 parks leftover remittance as `unapplied_cash`. #55 applies that leftover
to another open collection case. #57 refunds unused parked leftover. #112,
#113, and #114 persist those three money facts on PostgreSQL so GET
presentment survives process restart. `operator_console` already has
PaymentReceipt, CollectionCase, and CollectionCaseSettlement stories, plus
tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but
`STORYBOOK.md` has no story for parked leftover, leftover-apply, or
leftover-refund presentment.

Commercial money-fact persist is exhausted after #114: leftover refund,
leftover-apply, parked leftover, credit notes, spend_budget, dunning events,
webhook subscriptions, collection-dispute hold/release, collection write-off,
and collection-case settlement already reload. Remaining Memory-only gaps
are tenant API credentials (accepted later #84 control), AIS posting-receipt
observations, evaluation snapshots, #85, and production HA.

This repository is not the statutory accounting authority. Parked leftover,
leftover-apply, and leftover refund are presentation of remittance that is
not yet applied, applied residual, or returned cash, not posted journals
(IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider
secrets off this path (PCI Security Standards Council, 2024). Storybook is
the operator UI surface; this slice does not add a production SPA, login
wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Leftover HTTP write, GET, persist, and #24
`unapplied_cash.applied` / `refund.recorded` outbox contracts stay
unchanged.

## Decision

- Add a tokenized `UnappliedCash` module that reuses `AmountDue`,
  `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one same-tenant parked leftover, one
  leftover-apply, and one leftover-refund. The parked fixture validates
  against the existing unapplied-cash presentment schema. The apply
  fixture validates against the existing leftover-apply presentment
  schema. The refund fixture validates against the existing leftover-refund
  presentment schema. Do not invent a parallel envelope.
- Keep leftover amounts as exact-decimal strings. `AmountDue` presents the
  parked leftover, applied amount, or refund amount. Leftover-apply also
  presents current remaining. `StatusChip` presents `parked`, `applied`,
  or `recorded`. Next operator action is `wait` for park and refund, and
  `collect` for leftover-apply with residual remaining. Float money fails
  closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact leftover string.
- Do not change leftover park, apply, or refund HTTP writes. Do not add a
  GET side-effect. Do not persist evaluation snapshots, invent
  dimension-scoped budgets, or change leftover / credit-note / spend-budget
  / collection-dispute persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal, AIS
  call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory
  identifier.

## Consequences

- Operators can open one parked leftover, one leftover-apply, and one
  leftover-refund in Storybook and see exact leftover amounts, status,
  tenant pin, and the next action: wait or collect.
- Python remains the commercial authority. The console only presents
  stored #54/#55/#57 JSON.
- Leftover persist, HTTP write, GET, and #24 outbox stay unchanged. #85
  atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
