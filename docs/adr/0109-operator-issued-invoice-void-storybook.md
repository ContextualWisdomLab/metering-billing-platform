# ADR 0109: Operator Issued-Invoice Void Storybook

**Status:** Accepted

## Context

#41 issues one commercial `issued_invoice`. #63 records one unused
`issued_invoice_void`. #64 and #65 add the `invoice.voided` outbox and the
explicit void journal compose. #113 persists that unused void on PostgreSQL
so GET presentment survives process restart. `operator_console` already has
IssuedInvoice and unused issued-credit-note-void stories, plus tokenized
`AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no
story for unused issued-invoice-void presentment.

Commercial money-fact persist is exhausted after #114–#119: leftover
park/apply/refund, credit notes, spend_budget, dunning events, webhook
subscriptions, collection-dispute hold/release, collection write-off, and
collection-case settlement already reload. Remaining Memory-only gaps are
tenant API credentials (accepted later #84 control), AIS posting-receipt
observations, evaluation snapshots, #85, and production HA.

This repository is not the statutory accounting authority. An unused
issued-invoice void is presentation of unused issued consideration, not
reversed revenue or a posted journal (IFRS Foundation, 2024). IEEE 754
forbids smuggling binary floating-point values into money (IEEE, 2019).
PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI
Security Standards Council, 2024). Storybook is the operator UI surface;
this slice does not add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Issued-invoice, unused-void, HTTP write, GET,
persist, and #24 `invoice.voided` outbox contracts stay unchanged.

## Decision

- Add a tokenized `IssuedInvoiceVoid` module that reuses `AmountDue`,
  `StatusChip`, the tenant pin, and existing design tokens.
- Ship one Storybook story for one same-tenant unused issued-invoice void.
  The unused-void fixture validates against the existing
  issued-invoice-void presentment schema. Keep the existing IssuedInvoice
  stories as the issued presentment. Do not invent a parallel envelope.
- Keep `voided_amount` as an exact-decimal string. `AmountDue` presents
  that string. `StatusChip` presents `issued_invoice_void_status`
  `recorded`. Next operator action is `wait`. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The unused-void
  `issued_invoice_id` matches the existing taxed IssuedInvoice fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact inclusive voided string.
- Do not change `POST /v1/issued-invoices/{id}/voids`. Do not add a GET
  side-effect. Do not persist evaluation snapshots, invent
  dimension-scoped budgets, or change leftover / credit-note /
  spend-budget / collection-dispute / write-off persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal, AIS
  call, VAT/NTS adapter, `retained_earnings` or 310100, or statutory
  identifier.

## Consequences

- Operators can open one issued invoice and one unused issued-invoice void
  in Storybook and see exact inclusive voided amount, recorded status,
  tenant pin, and the next action: wait.
- Python remains the commercial authority. The console only presents
  stored #63 JSON.
- Issued-invoice-void persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
