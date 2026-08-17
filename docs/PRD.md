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

## Collection-case acceptance

- A known invoice draft opens one collection case whose outstanding equals the exact draft total.
- A second open of the same tenant and `invoice_draft_id` returns the same `collection_case_id`.
- Another tenant cannot see or collect the first tenant's case.
- Dunning events append commercial reminders (`first_notice`, `overdue_notice`) without capturing money.
- Missing drafts, cross-tenant IDs, float money, and zero outstanding fail closed.
- Status stays `open` or `dunning` until a receipt settles remaining outstanding to zero.

## Payment-intent acceptance

- A known collection case projects one payment intent whose amount equals the exact case outstanding.
- A second project of the same tenant, `collection_case_id`, source-payload hash, and contract version returns the same `payment_intent_id`.
- Another tenant cannot see or project the first tenant's case.
- Missing cases, cross-tenant IDs, float money, and zero amounts fail closed.
- Status stays `projected`, `cancelled`, or `rejected`. Operators next record a commercial receipt or cancel the intent.

## Payment-settlement acceptance

- A known projected intent records one payment receipt whose exact amount is applied against that intent.
- A full receipt of the intent amount zeros collection outstanding and marks the case `settled`.
- A partial receipt leaves residual outstanding and leaves the case `open` or `dunning`.
- A second receipt of the same tenant, `payment_intent_id`, received amount, source-payload hash, and contract version returns the same `payment_receipt_id`.
- Another tenant cannot see or settle the first tenant's intent.
- Cancel flips a projected intent to `cancelled` without writing a receipt or changing outstanding. Cancel replay is idempotent. A cancelled intent cannot later receive a receipt.
- Missing intents, cross-tenant IDs, float money, zero or negative amounts, over-application, and non-projected intents fail closed.
- Status stays `applied`. Operators next propose a cash journal to AIS, or record another partial receipt.

## Cash-journal acceptance

- A known payment receipt produces one balanced exact-decimal `accounting_journal_proposal` that debits `cash_receipt` and credits `accounts_receivable`.
- A second propose of the same tenant, `payment_receipt_id`, source-payload hash, and contract version returns the same `proposal_id`.
- Another tenant cannot see or propose from the first tenant's receipt.
- Missing receipts, cross-tenant IDs, float money, and zero or negative amounts fail closed.
- Collection outstanding is not changed. Status stays inside the proposal lifecycle and is never `posted`. Operators hand the persisted cash proposal to AIS.

## HTTP accept-surface acceptance

- Buyers and AIS can POST the already-built commercial path as JSON without importing in-process Python services.
- Every write requires `tenant_reference`. Money stays exact-decimal strings.
- HTTP 200 means `accepted` or `duplicate_replay`. HTTP 422 means `rejected` or an unreadable request. HTTP 404 is only an unknown route.
- HTTP does not post journals, store a card PAN, or add Stripe, Adyen, or Toss.

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
- Repository tooling, the usage-ingestion package, the windowed-rating package, invoice-draft, accounting-export, collection-case, payment-intent, payment-settlement, cash-journal export, and the HTTP accept surface reach 100% statement and branch coverage.
