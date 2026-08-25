# ADR 0107: Operator Collection Write-Off Storybook

**Status:** Accepted

## Context

#49 writes off leftover remaining on one same-tenant open collection case.
#46 remains the explicit settle-when-zero command after remaining is exact
zero. PostgreSQL already persists that write-off so GET presentment survives
process restart. `operator_console` already has CollectionCase and
CollectionCaseSettlement stories, plus tokenized `AmountDue`, `StatusChip`,
and tenant-pin modules, but `STORYBOOK.md` has no story for recorded
leftover-remaining write-off presentment.

Commercial money-fact persist is exhausted after #114–#117: leftover
park/apply/refund, credit notes, spend_budget, dunning events, webhook
subscriptions, collection-dispute hold/release, collection write-off, and
collection-case settlement already reload. Remaining Memory-only gaps are
tenant API credentials (accepted later #84 control), AIS posting-receipt
observations, evaluation snapshots, #85, and production HA.

This repository is not the statutory accounting authority. A commercial
write-off is presentation of leftover remaining consideration, not reversed
revenue or a posted journal (IFRS Foundation, 2024). IEEE 754 forbids
smuggling binary floating-point values into money (IEEE, 2019). PCI DSS
keeps card PAN, CVC, and provider secrets off this path (PCI Security
Standards Council, 2024). Storybook is the operator UI surface; this slice
does not add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Collection-write-off HTTP write, GET, persist,
and #24 `write_off.recorded` outbox contracts stay unchanged.

## Decision

- Add a tokenized `CollectionWriteOff` module that reuses `AmountDue`,
  `StatusChip`, the tenant pin, and existing design tokens.
- Ship one Storybook story for one same-tenant recorded leftover remaining
  write-off. The fixture validates against the existing collection-write-off
  presentment schema. Do not invent a parallel envelope.
- Keep `write_off_amount` and `remaining_outstanding_amount` as exact-decimal
  strings. `AmountDue` presents the leftover written off and the exact-zero
  remaining. `StatusChip` presents `collection_write_off_status` `recorded`.
  Case status stays `open`. Next operator action is `settle`. Float money
  fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact leftover and remaining strings.
- Do not change `POST /v1/collection-cases/{id}/write-offs`. Do not add a
  GET side-effect. Do not persist evaluation snapshots, invent
  dimension-scoped budgets, or change leftover / credit-note / spend-budget
  / collection-dispute persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal, AIS
  call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory
  identifier.

## Consequences

- Operators can open one recorded leftover remaining write-off in Storybook
  and see exact leftover written off, exact-zero remaining, open case
  status, tenant pin, and the next action: settle.
- Python remains the commercial authority. The console only presents stored
  #49 JSON.
- Collection-write-off persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
