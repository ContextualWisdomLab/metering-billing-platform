# ADR 0104: Operator Collection-Dispute Storybook

**Status:** Accepted

## Context

#66 holds one unused open or dunning collection case as an append-only
commercial `collection_dispute`. #67 releases that hold in place. #114
persists the hold and in-place release on PostgreSQL so GET presentment
survives process restart. `operator_console` already has CollectionCase
and CollectionCaseSettlement stories, plus tokenized `AmountDue`,
`StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no story for
the stored hold or released/fail-close presentment.

This repository is not the statutory accounting authority. A commercial
dispute is presentation of remaining consideration, not reversed revenue
or a posted journal (IFRS Foundation, 2024). IEEE 754 forbids smuggling
binary floating-point values into money (IEEE, 2019). PCI DSS keeps card
PAN, CVC, and provider secrets off this path (PCI Security Standards
Council, 2024). Storybook is the operator UI surface; this slice does not
add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots, #85 atomic
authorization, and leftover-refund Storybook stay later. Collection-dispute
HTTP write, GET, persist, and #24 `dispute.held` / `dispute.released`
outbox contracts stay unchanged.

## Decision

- Add a tokenized `CollectionDispute` module that reuses `AmountDue`,
  `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for one same-tenant held dispute and one
  released/fail-close of that same identity. The held fixture validates
  against the existing collection-dispute presentment schema. The
  released fixture validates against the existing collection-dispute
  release presentment schema. Do not invent a parallel envelope.
- Keep `remaining_outstanding_amount` as an exact-decimal string.
  `AmountDue` presents that string. `StatusChip` presents
  `collection_dispute_status` held versus released. Next operator action
  is `wait`. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact remaining.
- Do not change `POST /v1/collection-cases/{id}/disputes` or
  `POST /v1/collection-disputes/{id}/releases`. Do not add a GET
  side-effect. Do not persist evaluation snapshots, invent
  dimension-scoped budgets, or change leftover / credit-note / spend-budget
  persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal, AIS
  call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory
  identifier.

## Consequences

- Operators can open one held dispute and one released/fail-close in
  Storybook and see exact remaining, dispute status, tenant pin, and the
  next action: wait. A later hold of the released case still fail-closes
  as `collection_dispute_released`.
- Python remains the commercial authority. The console only presents
  stored #66/#67 JSON.
- Collection-dispute persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
