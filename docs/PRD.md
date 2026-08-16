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

## Initial milestone acceptance

- All three schemas declare Draft 2020-12 and pass the repository's offline conformance fixtures.
- Prompt and response text are rejected from usage events.
- `posted` is rejected from accounting proposal status.
- Accounting proposals fail semantic validation when line numbers repeat or debit and credit totals differ.
- Attribution and usage references are tenant-scoped by composite foreign keys.
- SQL object names satisfy the two-word `snake_case` rule.
- Mutable GitHub Action tags are rejected.
- Repository tooling reaches 100% statement and branch coverage.
