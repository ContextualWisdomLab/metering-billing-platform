# ADR 0119: Operator Leftover Journal Storybook

**Status:** Accepted

## Context

#58 / ADR 0057 composes one validated leftover
`accounting_journal_proposal` from a stored parked leftover. AIS pulls that
row through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. #122 / ADR 0112 persists that
proposal so GET presentment survives process restart. `operator_console`
already presents validated cash and invoice-draft journals through the
existing `JournalProposal` module, plus tokenized `AmountDue`, `StatusChip`,
and tenant-pin modules, but `STORYBOOK.md` has no leftover-journal fixture.
#116 already presents the parked leftover row. Existing leftover /
unapplied-cash Storybook stays that leftover-row presentment.

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
authorization stay later. Leftover journal HTTP compose, GET, persist, and
#24 `journal_proposal.validated` outbox contracts stay unchanged.

## Decision

- Reuse the existing `JournalProposal` module, `AmountDue`, `StatusChip`,
  the tenant pin, and existing design tokens. Do not invent a parallel
  envelope or money widget.
- Ship one Storybook story for one same-tenant validated leftover journal.
  The fixture validates against the existing
  accounting-journal-proposal schema. Keep the existing cash and
  invoice-draft `JournalProposal` stories as those presentments. Keep
  existing leftover / unapplied-cash Storybook as the leftover-row
  presentment.
- Fixture lines debit `cash_receipt` and credit `unapplied_cash` for the
  parked leftover exact string `0.001`. `proposal_status` stays
  `validated`. Parked leftover stays `parked`. Next operator action copy
  is `wait`. The published journal-proposal contract has no
  `next_operator_action` field. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The leftover-journal
  `source_event_references` leftover matches the existing parked leftover
  UnappliedCash fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact leftover debit string.
- Do not change `POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals`
  or GET journal-proposal routes. Do not add a GET side-effect. Do not
  persist evaluation snapshots, invent dimension-scoped budgets, or change
  leftover / credit-note / spend-budget / collection-dispute / write-off
  persist.
- Do not implement leftover-apply journal Storybook, leftover-refund
  journal Storybook, write-off journal Storybook, unused void journal
  Storybook, atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal compose
  write, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or
  statutory identifier.

## Consequences

- Operators can open one validated leftover journal in Storybook and see
  exact leftover debit, unapplied-cash credit, validated status, tenant
  pin, and the next action: wait. Cash and invoice-draft journal stories
  stay those presentments. Leftover / unapplied-cash Storybook stays the
  leftover-row presentment.
- Python remains the commercial authority. The console only presents
  stored #58 JSON.
- Leftover journal persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
