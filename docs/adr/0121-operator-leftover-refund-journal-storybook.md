# ADR 0121: Operator Leftover-Refund Journal Storybook

**Status:** Accepted

## Context

#59 / ADR 0056 composes one validated leftover-refund
`accounting_journal_proposal` from a stored leftover refund. AIS pulls that
row through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. #125 / ADR 0114 persists that
proposal so GET presentment survives process restart. `operator_console`
already presents validated cash, invoice-draft, leftover, and leftover-apply
journals through the existing `JournalProposal` module, plus tokenized
`AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no
leftover-refund journal fixture. #116 already presents the leftover-refund
row. Existing leftover-refund UnappliedCash Storybook stays that leftover-refund
row presentment.

Already-specified journal persist is exhausted after #128: cash, credit,
write-off, leftover, leftover-apply, leftover-refund, unused invoice-void,
unused credit-note-void, and invoice-draft journals already reload.
Remaining Memory-only gaps are tenant API credentials (accepted later #84
control), AIS posting-receipt observations, evaluation snapshots, #85, and
production HA.

This repository is not the statutory accounting authority. A validated
journal proposal is presentation of a billing-owned proposal for AIS to
pull, not a posted journal (IFRS Foundation, 2024). IEEE 754 forbids
smuggling binary floating-point values into money (IEEE, 2019). PCI DSS
keeps card PAN, CVC, and provider secrets off this path (PCI Security
Standards Council, 2024). Storybook is the operator UI surface; this slice
does not add a production SPA, login wall, Stripe, or AIS call.

Issue #84 remains the broader durable-runtime backlog. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later. Leftover-refund journal HTTP compose, GET,
persist, and #24 `journal_proposal.validated` outbox contracts stay
unchanged.

## Decision

- Reuse the existing `JournalProposal` module, `AmountDue`, `StatusChip`,
  the tenant pin, and existing design tokens. Do not invent a parallel
  envelope or money widget.
- Ship one Storybook story for one same-tenant validated leftover-refund
  journal. The fixture validates against the existing
  accounting-journal-proposal schema. Keep the existing cash, invoice-draft,
  leftover, and leftover-apply `JournalProposal` stories as those
  presentments. Keep existing leftover-refund UnappliedCash Storybook as the
  leftover-refund row presentment.
- Fixture lines debit `unapplied_cash` and credit `cash_receipt` for the
  refunded leftover exact string `0.001` from
  `propose_refund_journal` / `test_unapplied_cash_refund_journal_is_durable`.
  `proposal_status` stays `validated`. Leftover stays `parked`. Leftover-apply
  remaining stays `19.999`. Next operator action copy is `wait`. The
  published journal-proposal contract has no `next_operator_action` field.
  Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The leftover-refund
  journal `source_event_references` leftover-refund matches the existing
  leftover-refund UnappliedCash fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact leftover-refund credit string.
- Do not change
  `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals`
  or GET journal-proposal routes. Do not add a GET side-effect. Do not
  persist evaluation snapshots, invent dimension-scoped budgets, or change
  leftover / credit-note / spend-budget / collection-dispute / write-off
  persist.
- Do not implement write-off journal Storybook, unused void journal
  Storybook, atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal compose
  write, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or
  statutory identifier. Do not re-apply, refund again, settle, or void. Do
  not flip `proposal_status` to `posted`.

## Consequences

- Operators can open one validated leftover-refund journal in Storybook and
  see exact unapplied-cash debit, cash-receipt credit, validated status,
  tenant pin, and the next action: wait. Cash, invoice-draft, leftover, and
  leftover-apply journal stories stay those presentments. Leftover-refund
  UnappliedCash Storybook stays the leftover-refund row presentment. Leftover
  stays `parked`. Leftover-apply remaining stays `19.999`.
- Python remains the commercial authority. The console only presents
  stored #59 JSON.
- Leftover-refund journal persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
