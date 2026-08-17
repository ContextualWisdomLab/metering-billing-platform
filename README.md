# Metering Billing Platform

CWL's provider-neutral commercial control plane for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

## What this product is

The Metering Billing Platform is the commercial system of record for CWL product usage and billing. Product services emit usage. Operators configure meters, prices, budgets, and collection providers. Finance operations review invoice intent, settlement, and accounting exports. Customers inspect usage and spend by product, project, principal, and credential.

The control plane owns these commercial facts:

- usage attribution to a billing account, principal, credential, project, and cost center;
- versioned meters, prices, contracts, and deterministic rating outcomes;
- entitlements, quotas, credits, and spend authorization;
- invoice intent and explainable invoice lines;
- payment-provider projection, settlement evidence, and commercial reconciliation.

It does not implement a payment provider, a merchant of record, or a general ledger. Those remain replaceable adapters or a separate accounting authority.

## Authority

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

See [docs/ACCOUNTING_BOUNDARY.md](docs/ACCOUNTING_BOUNDARY.md) and [ADR 0002](docs/adr/0002-accounting-boundary.md).

## Operator start

1. Read the product and architecture contracts: [docs/PRD.md](docs/PRD.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and [docs/DATA_MODEL.md](docs/DATA_MODEL.md).
2. Treat [docs/ACCOUNTING_BOUNDARY.md](docs/ACCOUNTING_BOUNDARY.md) as the billing-versus-books split.
3. Use the accepted ADRs for commercial authority, the accounting boundary, and invoice-intent handoff:
   - [docs/adr/0001-commercial-authority.md](docs/adr/0001-commercial-authority.md)
   - [docs/adr/0002-accounting-boundary.md](docs/adr/0002-accounting-boundary.md)
   - [docs/adr/0003-invoice-intent-and-revenue.md](docs/adr/0003-invoice-intent-and-revenue.md)
4. Validate a local checkout with the commands in [CONTRIBUTING.md](CONTRIBUTING.md). Command dumps live in [docs/doctoring/VALIDATION.md](docs/doctoring/VALIDATION.md).

The initial foundation is contract-first: closed JSON Schema payloads, a normalized PostgreSQL core, and offline repository validation. Runtime ingestion, rating, and provider adapters are subsequent increments.

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
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local validation, exact-head CI, and successor-PR order |
