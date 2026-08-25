# ADR 0122: Operator Write-Off Journal Storybook

**Status:** Accepted

## Context

#51 / ADR 0048 composes one validated write-off
`accounting_journal_proposal` from a stored collection write-off. AIS pulls
that row through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. #122 / ADR 0111 persists that
proposal so GET presentment survives process restart.
`find_journal_proposal_for_write_off` and
`test_write_off_journal_is_durable` already reload the write-off journal.
`operator_console` already presents validated cash, invoice-draft,
leftover, leftover-apply, and leftover-refund journals through the
existing `JournalProposal` module, plus tokenized `AmountDue`,
`StatusChip`, and tenant-pin modules, but `STORYBOOK.md` has no write-off
journal fixture. #118 already presents the leftover remaining write-off
row. Existing collection-write-off row Storybook stays that write-off row
presentment.

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
authorization stay later. Write-off journal HTTP compose, GET, persist,
and #24 `journal_proposal.validated` outbox contracts stay unchanged.

## Decision

- Reuse the existing `JournalProposal` module, `AmountDue`, `StatusChip`,
  the tenant pin, and existing design tokens. Do not invent a parallel
  envelope or money widget.
- Ship one Storybook story for one same-tenant validated write-off
  journal. The fixture validates against the existing
  accounting-journal-proposal schema. Keep the existing cash,
  invoice-draft, leftover, leftover-apply, and leftover-refund
  `JournalProposal` stories as those presentments. Keep existing
  collection-write-off Storybook as the write-off row presentment.
- Fixture lines debit `write_off_expense` and credit
  `accounts_receivable` for the written-off leftover remaining exact
  string `0.001` from `propose_write_off_journal` leftover compose /
  `test_write_off_journal_is_durable`. `proposal_status` stays
  `validated`. Write-off remaining stays exact `0`. Next operator action
  copy is `wait`. The published journal-proposal contract has no
  `next_operator_action` field. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The write-off
  journal `source_event_references` write-off matches the existing
  leftover remaining CollectionWriteOff fixture.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact write-off-expense debit string.
- Do not change
  `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals`
  or GET journal-proposal routes. Do not add a GET side-effect. Do not
  persist evaluation snapshots, invent dimension-scoped budgets, or change
  leftover / credit-note / spend-budget / collection-dispute / write-off
  persist.
- Do not implement unused invoice-void journal Storybook, unused
  credit-note-void journal Storybook, atomic authorization, quotas,
  entitlements, reserve/commit/release, or a hard-stop. Do not invent a
  journal compose write, AIS call, VAT/NTS adapter, `retained_earnings` or
  310100, or statutory identifier. Do not settle or void. Do not flip
  `proposal_status` to `posted`.

## Consequences

- Operators can open one validated write-off journal in Storybook and see
  exact write-off-expense debit, AR credit, validated status, tenant pin,
  and the next action: wait. Cash, invoice-draft, leftover,
  leftover-apply, and leftover-refund journal stories stay those
  presentments. Collection-write-off Storybook stays the write-off row
  presentment. Write-off remaining stays exact `0`.
- Python remains the commercial authority. The console only presents
  stored #51 JSON.
- Write-off journal persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
