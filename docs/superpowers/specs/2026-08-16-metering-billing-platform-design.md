# Metering Billing Platform Initial Foundation Design

**Status:** Accepted through the user's request to create the repository and open the initial pull request.

## Purpose

The Metering Billing Platform is the CWL commercial system of record for usage attribution, metering, pricing, rating, entitlements, invoice intent, payment-provider projection, and reconciliation. It is not the statutory accounting system and must not become the authoritative general ledger.

## Product boundary

The platform owns:

- billing accounts, principals, credential attribution, and cost centers;
- immutable usage events and corrections;
- versioned meters, prices, contracts, and rating outcomes;
- credits, quotas, spend authorizations, and invoice intent;
- provider mappings, webhook evidence, settlement evidence, and reconciliation;
- operational billing-ledger evidence sufficient to explain every charge.

The platform does not own:

- the chart of accounts, legal books, fiscal periods, statutory journals, trial balance, financial close, consolidation, or statutory financial statements;
- final revenue-recognition policy, tax accounting, foreign-currency translation policy, or regulatory filing;
- card data or payment credentials held by a payment provider;
- identity credentials owned by Keyverse or the credential issuer.

## Accounting integration

The billing platform exports immutable, idempotent `accounting_journal_proposal` documents. A proposal is balanced in its transaction currency and carries source evidence, but it is never a posted journal. A separate Accounting Information Platform validates accounting policy, resolves legal entity and book, maps accounts and dimensions, applies fiscal-period controls, posts or rejects the journal, and returns a posting receipt.

The boundary is deliberately asymmetric:

```text
metering-billing-platform
  authoritative usage, rating, invoice, payment and settlement facts
       |
       | accounting_journal_proposal
       v
accounting-information-platform
  authoritative chart of accounts, books, journals, close and statements
       |
       | accounting_posting_receipt
       v
metering-billing-platform
  reconciliation reference only; no accounting state override
```

## Initial pull request scope

The first pull request establishes contracts rather than a broad application:

1. Repository governance, architecture, data model, and accounting boundary.
2. JSON Schema contracts for usage events, provider capabilities, and accounting journal proposals.
3. A normalized PostgreSQL migration for the minimum attribution, usage, meter, provider-mapping, and accounting-export records.
4. Offline repository validation with no third-party runtime dependency.
5. A pinned GitHub Actions workflow that exercises all repository-contract code with 100% line and branch coverage.

## Invariants

1. Provider identifiers are never internal primary keys.
2. Credentials are attribution dimensions, not customers.
3. Usage, provider cost, rated charge, invoice, payment, settlement, and accounting posting are distinct facts.
4. Estimated usage is not billable by default.
5. Corrections append compensating facts rather than mutate historical facts.
6. Billing providers and merchant-of-record services are adapters.
7. A webhook is external evidence until verified and normalized.
8. An accounting proposal can be exported or rejected but cannot claim to be posted.
9. Every exported proposal is idempotent and traceable to source facts.
10. Database objects use two-or-more-word `snake_case` names and normalized relations.

## Deferred work

Runtime APIs, deterministic rating, credits, spend reservation, invoice generation, provider adapters, a live PostgreSQL integration suite, and the separate Accounting Information Platform are subsequent reviewed increments.
