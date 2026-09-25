# ADR 0123: Operator Unused Invoice-Void Journal Storybook

**Status:** Accepted

## Context

#63 / ADR 0062 composes one validated unused invoice-void
`accounting_journal_proposal` from a stored unused issued-invoice void. AIS
pulls that row through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. #126 / ADR 0115 persists that
proposal so GET presentment survives process restart.
`find_journal_proposal_for_issued_invoice_void` and
`test_issued_invoice_void_journal_is_durable` already reload the unused
invoice-void journal. `operator_console` already presents validated cash,
invoice-draft, leftover, leftover-apply, leftover-refund, and write-off
journals through the existing `JournalProposal` module, plus tokenized
`AmountDue`, `StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has
no unused invoice-void journal fixture. #120 already presents the unused
issued-invoice void row. Existing unused issued-invoice-void row Storybook
stays that unused-void row presentment. Existing write-off journal
Storybook stays the write-off journal presentment.

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
authorization stay later. Unused invoice-void journal HTTP compose, GET,
persist, and #24 `journal_proposal.validated` outbox contracts stay
unchanged.

## Decision

- Reuse the existing `JournalProposal` module, `AmountDue`, `StatusChip`,
  the tenant pin, and existing design tokens. Do not invent a parallel
  envelope or money widget.
- Ship one Storybook story for one same-tenant validated unused
  invoice-void journal. The fixture validates against the existing
  accounting-journal-proposal schema. Keep the existing cash,
  invoice-draft, leftover, leftover-apply, leftover-refund, and write-off
  `JournalProposal` stories as those presentments. Keep existing unused
  issued-invoice-void Storybook as the unused-void row presentment.
- Fixture lines reverse the taxed invoice-draft journal: debit
  `usage_revenue` exclusive `100.00`, debit `tax_payable` tax `10.00`, and
  credit `accounts_receivable` inclusive `110.00` from
  `propose_void_journal` leftover compose /
  `test_issued_invoice_void_journal_is_durable` /
  `test_taxed_void_reverses_tax_payable_on_the_same_journal`.
  `proposal_status` stays `validated`. Unused issued-invoice void inclusive
  `voided_amount` stays `110.00`. Next operator action copy is `wait`. The
  published journal-proposal contract has no `next_operator_action` field.
  Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The unused
  invoice-void journal `source_event_references` void matches the existing
  unused issued-invoice void fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact usage-revenue debit string.
- Do not change
  `POST /v1/issued-invoice-voids/{issued_invoice_void_id}/journal-proposals`
  or GET journal-proposal routes. Do not add a GET side-effect. Do not
  persist evaluation snapshots, invent dimension-scoped budgets, or change
  leftover / credit-note / spend-budget / collection-dispute / write-off
  persist.
- Do not implement unused credit-note-void journal Storybook, atomic
  authorization, quotas, entitlements, reserve/commit/release, or a
  hard-stop. Do not invent a journal compose write, AIS call, VAT/NTS
  adapter, `retained_earnings` or 310100, or statutory identifier. Do not
  settle or void. Do not flip `proposal_status` to `posted`.

## Consequences

- Operators can open one validated unused invoice-void journal in
  Storybook and see exact usage-revenue debit, tax-payable debit, AR
  credit, validated status, tenant pin, and the next action: wait. Cash,
  invoice-draft, leftover, leftover-apply, leftover-refund, and write-off
  journal stories stay those presentments. Unused issued-invoice-void
  Storybook stays the unused-void row presentment. Unused issued-invoice
  void inclusive `voided_amount` stays `110.00`.
- Python remains the commercial authority. The console only presents
  stored #63 JSON.
- Unused invoice-void journal persist, HTTP write, GET, and #24 outbox
  stay unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
