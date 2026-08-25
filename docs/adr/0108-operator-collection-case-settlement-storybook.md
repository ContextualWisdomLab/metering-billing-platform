# ADR 0108: Operator Collection-Case-Settlement Storybook

**Status:** Accepted

## Context

#49 writes off leftover remaining on one same-tenant open collection case
and leaves remaining exact zero with case status still `open`. #46 is the
explicit settle-when-zero command after remaining is exact zero. PostgreSQL
already persists that settlement so GET presentment survives process
restart. `operator_console` already has a CollectionCaseSettlement module
plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, and
`STORYBOOK.md` already lists a generic morning-zero settlement. #118 added
the leftover-remaining write-off story whose next operator action is
`settle`. The leftover write-off case itself had no settle-when-zero
presentment.

Commercial money-fact persist is exhausted after #114–#118: leftover
park/apply/refund, credit notes, spend_budget, dunning events, webhook
subscriptions, collection-dispute hold/release, collection write-off, and
collection-case settlement already reload. Remaining Memory-only gaps are
tenant API credentials (accepted later #84 control), AIS posting-receipt
observations, evaluation snapshots, #85, and production HA.

This repository is not the statutory accounting authority. An explicit
settle-when-zero is presentation of exact-zero remaining consideration, not
reversed revenue or a posted journal (IFRS Foundation, 2024). IEEE 754
forbids smuggling binary floating-point values into money (IEEE, 2019).
PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI
Security Standards Council, 2024). Storybook is the operator UI surface;
this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Collection-case-settlement HTTP write, GET,
persist, and #24 `collection.settled` outbox contracts stay unchanged.

## Decision

- Reuse the tokenized `CollectionCaseSettlement` module that already
  composes `AmountDue`, `StatusChip`, the tenant pin, and existing design
  tokens. Do not invent a second money widget. `AmountDue` stays the
  reused tokenized module.
- Ship one Storybook story for one settle-when-zero of the same-tenant
  leftover write-off case at exact-zero remaining. The fixture validates
  against the existing collection-case-settlement presentment schema. Keep
  the existing morning-zero settlement story. Do not invent a parallel
  envelope.
- Keep `remaining_outstanding_amount` as an exact-decimal `0`. `AmountDue`
  presents that string. `StatusChip` presents `collection_case_settlement_status`
  `settled`. Case status is `settled`. Next operator action is `wait`.
  Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The settlement
  `collection_case_id` matches the leftover write-off fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact-zero remaining string.
- Do not change `POST /v1/collection-cases/{id}/settlements`. Do not add a
  GET side-effect. Do not persist evaluation snapshots, invent
  dimension-scoped budgets, or change leftover / credit-note / spend-budget
  / collection-dispute / write-off persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal, AIS
  call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory
  identifier.

## Consequences

- Operators can open the leftover write-off, then the explicit
  settle-when-zero of that same case, and see exact-zero remaining,
  settled status, tenant pin, and the next action: wait.
- Python remains the commercial authority. The console only presents
  stored #46 JSON.
- Collection-case-settlement persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
