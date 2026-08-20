# Metering Billing Platform

CWL's provider-neutral commercial control plane for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

## Current release boundary

This repository remains a foundation / bootstrap. The default branch does not ship a runtime, HTTP API, SDK, or payment-provider adapter. An open pull request, local fixture, or green unit job is not production evidence; the cumulative candidate is not shipped until independent review and terminal checks complete.

Naruon is an optional CWL composition host, not a boot dependency. This platform is specified to run independently and does not require a Naruon checkout, process, or configuration.

## What this product owns

The control plane owns these commercial facts:

- usage attribution to a billing account, principal, credential, project, and cost center;
- versioned meters, prices, contracts, and deterministic rating outcomes;
- entitlements, quotas, credits, and spend authorization;
- invoice intent and explainable invoice lines;
- payment-provider projection, settlement evidence, and commercial reconciliation.

It does not implement a payment provider, a merchant of record, or a general ledger. Those remain replaceable adapters or a separate accounting authority.

The foundation product contract requires at-least-once delivery to produce at-most-once monetary effects, estimated usage to remain non-billable until accepted, immutable historical rating inputs, provider-object mappings, provider-sticky settlement facts, and usage evidence for every invoice line. These are contracts, not a claim that the default branch already implements them.

## Authority boundary

This repository owns commercial usage and billing truth. It does **not** own the statutory chart of accounts, legal books, posted journals, fiscal close, consolidation, or financial statements. Those belong to a separate Accounting Information Platform.

```text
CWL products
  -> canonical usage events
  -> Metering Billing Platform
  -> invoice and settlement facts
  -> accounting journal proposals
  -> Accounting Information Platform
  -> posted journals and financial statements
```

Billing exports balanced journal *proposals*. Accounting decides policy, maps accounts, posts or rejects, and returns a posting receipt. A webhook cannot grant entitlement or post a statutory journal. An accounting rejection cannot rewrite measured usage or a customer invoice.

See [docs/ACCOUNTING_BOUNDARY.md](docs/ACCOUNTING_BOUNDARY.md), [ADR 0002](docs/adr/0002-accounting-boundary.md), and the claimed-standard traceability and APA 7 references under [docs/doctoring/](docs/doctoring/).

## Independent operation and sibling integration

No runtime URL, authentication path, or SDK is published until the corresponding contract is actually present on the target branch. Identity authentication remains with Keyverse or the credential issuer. Credentials are attribution dimensions, not customers; provider object IDs remain behind mapping records and are not internal primary keys.

The intended first producer is `contextual-orchestrator`, which emits the versioned usage-event contract. The accounting-information-platform consumes journal proposals and returns posting receipts; this repository must not mark proposals as statutory posted journals.

The default branch is offline and contract-first. Runtime ingestion, rating, provider adapters, live PostgreSQL verification, production identity, and browser acceptance remain later increments unless their reviewed code is on the target branch.

## Operator next actions

1. Read the product, technical, architecture, data-model, and accounting-boundary contracts linked below.
2. Follow [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for local validation and review order.
3. Use [docs/doctoring/VALIDATION.md](docs/doctoring/VALIDATION.md) for the latest verified commands and keep local evidence separate from hosted gate evidence.

## Documentation map

| Document | Use |
| --- | --- |
| [docs/PRD.md](docs/PRD.md) | Product outcome, users, and acceptance properties |
| [docs/TRD.md](docs/TRD.md) | Contract, persistence, provider, and accounting planes |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Bounded contexts and authority matrix |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Initial normalized records and monetary rules |
| [docs/ACCOUNTING_BOUNDARY.md](docs/ACCOUNTING_BOUNDARY.md) | Proposal export and posting-receipt contract |
| [docs/doctoring/STANDARD_TRACEABILITY.md](docs/doctoring/STANDARD_TRACEABILITY.md) | Claimed standards mapped to engineering decisions |
| [docs/doctoring/REFERENCES.md](docs/doctoring/REFERENCES.md) | APA citations for claimed standards only |
| [docs/product-technical-gap-baseline.md](docs/product-technical-gap-baseline.md) | Verified product and technical gap inventory |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local validation, exact-head CI, and successor-PR order |
