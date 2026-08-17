# Product Requirements Document

## Product outcome

Organizations can attribute AI-platform and CWL-product usage to a billing account, principal, credential, project, and cost center; apply versioned commercial rules; control spend; explain charges; and project those results to replaceable collection providers.

## Primary users

- Platform operators configure meters, prices, budgets, provider accounts, and reconciliation.
- Finance operations review invoice intent, collections, refunds, settlement, and accounting exports.
- Customer administrators inspect usage and spend by product, project, principal, and credential.
- Product services emit usage without implementing price or accounting logic.

## Required product properties

1. At-least-once event delivery produces at-most-once monetary effects.
2. Estimated usage is not automatically billable.
3. Price, contract, and meter changes do not rewrite historical rating outcomes.
4. Provider customer and subscription identifiers stay behind mapping records.
5. Payment, refund, dispute, and settlement facts remain provider-sticky after creation.
6. Every invoice line is explainable down to its usage evidence.
7. Accounting exports are proposals and cannot claim statutory posting.

## First commercial vertical

```text
contextual-orchestrator usage
-> canonical usage event
-> billability decision
-> deterministic aggregate
-> invoice intent
-> manual enterprise invoice or Lemon Squeezy projection
-> payment and settlement evidence
-> reconciliation
-> accounting journal proposal
```

## Windowed-rating acceptance

- A known stored-usage set in a half-open ISO 8601 window produces one exact invoice-intent money total.
- Equivalent decimal and UTC spellings (`1` vs `1.0`, `Z` vs `+00:00`) remain one stored fact and therefore one rated quantity.
- A second rate of the same tenant, window, rate-card version, and usage snapshot returns the same `rating_run_id` and totals.
- Another tenant's usage is invisible to the rated total.
- Estimated, reconstructed, and other non-billable qualities stay stored and stay out of invoice-intent money when `meter_quality_rule` says so.
- Rating does not create an invoice draft, a payment-provider command, or a posted accounting journal.

## Journal-proposal acceptance

- A known invoice draft produces one balanced exact-decimal `accounting_journal_proposal` whose debit total equals its credit total.
- A second propose of the same tenant, `invoice_draft_id`, source-payload hash, and contract version returns the same `proposal_id`.
- Another tenant cannot see or propose from the first tenant's draft.
- Missing drafts, zero draft totals, float money, and unbalanced lines fail closed.
- Status stays inside the proposal lifecycle and is never `posted`. Operators hand the proposal to AIS.

## Usage-ingestion acceptance

- A known event batch stores one usage set; replaying the same batch returns `duplicate_replay` and does not grow that set.
- A replay with the same source-event key and a different source-payload hash or contract version is rejected.
- A usage event cannot attribute a billing account, principal, or credential from another tenant.
- Measurement quantities persist as exact decimals, never as binary floating-point values.
- Time-window queries return only the tenant's events whose `occurred_at` lies in `[window_started_at, window_ended_at)`.
- Ingestion does not create a posted accounting journal.

## Initial milestone acceptance

- All published schemas declare Draft 2020-12 and pass the repository's offline conformance fixtures.
- Prompt and response text are rejected from usage events.
- `posted` is rejected from accounting proposal status.
- Accounting proposals fail semantic validation when line numbers repeat or debit and credit totals differ.
- Attribution and usage references are tenant-scoped by composite foreign keys.
- SQL object names satisfy the two-word `snake_case` rule.
- Mutable GitHub Action tags are rejected.
- Repository tooling, the usage-ingestion package, the windowed-rating package, invoice-draft, and accounting-export reach 100% statement and branch coverage.
