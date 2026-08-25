# ADR 0110: Operator Journal-Proposal Storybook

**Status:** Accepted

## Context

#13 composes one validated cash `accounting_journal_proposal` from a stored
`payment_receipt`. AIS pulls that row through existing
`GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
PostgreSQL already persists the proposal so GET presentment survives process
restart. `operator_console` already has PaymentReceipt and WebhookOutboxEvent
stories, plus tokenized `AmountDue`, `StatusChip`, and tenant-pin modules, but
`STORYBOOK.md` has no story for the stored journal-proposal GET.

Commercial money-fact persist is exhausted after #114–#120: leftover
park/apply/refund, credit notes, spend_budget, dunning events, webhook
subscriptions, collection-dispute hold/release, collection write-off,
collection-case settlement, and unused invoice/credit voids already reload.
Remaining Memory-only gaps are tenant API credentials (accepted later #84
control), AIS posting-receipt observations, evaluation snapshots, #85, and
production HA. DunningNotice, PaymentReceipt, and PaymentIntent Storybooks
already exist.

This repository is not the statutory accounting authority. A validated
journal proposal is presentation of a billing-owned proposal for AIS to
pull, not a posted journal (IFRS Foundation, 2024). IEEE 754 forbids
smuggling binary floating-point values into money (IEEE, 2019). PCI DSS
keeps card PAN, CVC, and provider secrets off this path (PCI Security
Standards Council, 2024). Storybook is the operator UI surface; this slice
does not add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Journal-proposal HTTP compose, GET, persist, and
#24 `journal_proposal.validated` outbox contracts stay unchanged.

## Decision

- Add a tokenized `JournalProposal` module that reuses `AmountDue`,
  `StatusChip`, the tenant pin, and existing design tokens.
- Ship one Storybook story for one same-tenant validated morning cash
  journal. The fixture validates against the existing
  accounting-journal-proposal schema. Keep PaymentReceipt as the receipt
  presentment and WebhookOutboxEvent as the outbox presentment. Do not
  invent a parallel envelope.
- Keep line `debit_amount` and `credit_amount` as exact-decimal strings.
  `AmountDue` presents the cash-receipt debit string. `StatusChip` presents
  `proposal_status` `validated`. Next operator action copy is `wait`. The
  published journal-proposal contract has no `next_operator_action` field.
  Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The cash-journal
  `source_event_references` receipt matches the existing full PaymentReceipt
  fixture. `proposal_id` matches the existing pending
  `journal_proposal.validated` outbox `source_id`.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact received string.
- Do not change `POST /v1/cash-journal-proposals` or GET journal-proposal
  routes. Do not add a GET side-effect. Do not persist evaluation
  snapshots, invent dimension-scoped budgets, or change leftover /
  credit-note / spend-budget / collection-dispute / write-off persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal compose
  write, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or
  statutory identifier.

## Consequences

- Operators can open one applied morning receipt and one validated cash
  journal in Storybook and see exact received amount, validated status,
  tenant pin, and the next action: wait.
- Python remains the commercial authority. The console only presents
  stored #13 JSON.
- Journal-proposal persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
