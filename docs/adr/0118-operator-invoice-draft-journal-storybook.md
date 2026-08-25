# ADR 0118: Operator Invoice-Draft Journal Storybook

**Status:** Accepted

## Context

#8 / ADR 0006 composes one validated invoice-draft
`accounting_journal_proposal` from a stored `invoice_draft`. AIS pulls that
row through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. #128 / ADR 0117 persists that
proposal so GET presentment survives process restart. `operator_console`
already presents one validated morning cash journal through the existing
`JournalProposal` module, plus tokenized `AmountDue`, `StatusChip`, and
tenant-pin modules, but `STORYBOOK.md` has no invoice-draft journal fixture.

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
authorization stay later. Invoice-draft journal HTTP compose, GET, persist,
and #24 `journal_proposal.validated` outbox contracts stay unchanged.

## Decision

- Reuse the existing `JournalProposal` module, `AmountDue`, `StatusChip`,
  the tenant pin, and existing design tokens. Do not invent a parallel
  envelope or money widget.
- Ship one Storybook story family for same-tenant validated invoice-draft
  journals. Fixtures validate against the existing
  accounting-journal-proposal schema. Keep the existing cash
  `JournalProposal` story as the cash presentment.
- Untaxed fixture lines debit `accounts_receivable` and credit
  `usage_revenue` for the known morning exact string `0.003705`. Taxed
  fixture lines debit `accounts_receivable` inclusive `110.00`, credit
  `usage_revenue` exclusive `100.00`, and credit `tax_payable` tax
  `10.00`. `proposal_status` stays `validated`. Next operator action copy
  is `wait`. The published journal-proposal contract has no
  `next_operator_action` field. Float money fails closed.
- Pin fixture `X-CWL-Tenant-Reference` to the commercial `tenant_account`
  (`urn:cwl:tenant_001`). Do not auto-create tenants. The morning
  invoice-draft `source_event_references` draft matches the existing
  untaxed InvoiceStatement fixture. The taxed invoice-draft
  `source_event_references` draft matches the existing taxed IssuedInvoice
  and morning VAT TaxAssessment fixtures.
- Do not invent BudgetGauge, a notify percentage, or a new money widget.
  `AmountDue` already presents the exact AR debit string.
- Do not change `POST` invoice-draft journal compose or GET
  journal-proposal routes. Do not add a GET side-effect. Do not persist
  evaluation snapshots, invent dimension-scoped budgets, or change leftover
  / credit-note / spend-budget / collection-dispute / write-off persist.
- Do not implement atomic authorization, quotas, entitlements,
  reserve/commit/release, or a hard-stop. Do not invent a journal compose
  write, AIS call, VAT/NTS adapter, `retained_earnings` or 310100, or
  statutory identifier.

## Consequences

- Operators can open one validated morning invoice-draft journal and one
  validated taxed invoice-draft journal in Storybook and see exact AR,
  revenue, optional tax-payable, validated status, tenant pin, and the
  next action: wait. The cash journal story stays the cash presentment.
- Python remains the commercial authority. The console only presents
  stored #8 JSON.
- Invoice-draft journal persist, HTTP write, GET, and #24 outbox stay
  unchanged. #85 atomic authorization remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
