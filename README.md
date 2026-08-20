# Metering Billing Platform

CWL's provider-neutral commercial control plane for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

## Current release boundary

This repository remains a foundation / bootstrap. The default branch does not ship a runtime, HTTP API, SDK, or payment-provider adapter. The cumulative implementation is being carried through reviewed pull requests; an open pull request, local fixture, or green unit job is not production evidence.

Naruon is an optional CWL composition host, not a boot dependency. This platform is specified to run independently and does not require a Naruon checkout, process, or configuration.

## What this product owns

The control plane owns these commercial facts:

- usage attribution to a billing account, principal, credential, project, and cost center;
- versioned meters, prices, contracts, and deterministic rating outcomes;
- entitlements, quotas, credits, and spend authorization;
- invoice intent and explainable invoice lines;
- payment-provider projection, settlement evidence, and commercial reconciliation.

It does not implement a payment provider, a merchant of record, or a general ledger. Those remain replaceable adapters or a separate accounting authority.

The foundation PRD also requires at-least-once delivery to produce at-most-once monetary effects, estimated usage to remain non-billable until accepted, immutable historical rating inputs, provider-object mappings, provider-sticky settlement facts, and usage evidence for every invoice line. These are product contracts, not a claim that the default branch already implements them.

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

## Additional buyer and integration notes

Identity authentication remains with Keyverse or the credential issuer. Credentials are attribution dimensions, not customers. Payment processors, merchant-of-record services, and gateways are replaceable capability adapters; their object IDs are not internal primary keys.

## Current contents

| Location | What exists |
| --- | --- |
| Default branch (`develop`, also `main`) | This buyer/operator README and contributor operation notes. No application code. |
| Open [PR #1](https://github.com/ContextualWisdomLab/metering-billing-platform/pull/1) | PRD, TRD, architecture, data model, accounting boundary, two accepted ADRs, APA 7th reference list, JSON Schema Draft 2020-12 contracts, PostgreSQL 18 core migration, offline repository validator, exact-head CI. |
| Not present in this repository | A bindable HTTP server, OpenAPI/AsyncAPI, live PostgreSQL suite, rating engine, invoice API, provider webhook receiver, or published sibling SDK. |

## Independent run

This repository runs without Naruon and without any sibling checkout. There is no compose file, environment template, or process to start on the default branch.

```bash
git clone https://github.com/ContextualWisdomLab/metering-billing-platform.git
cd metering-billing-platform
```

That clone is the whole local product surface on the default branch: this README and `docs/`.

The foundation PR defines additional offline commands. They contact no external service, do not start a billing API, and do not require Naruon, contextual-orchestrator, or accounting-information-platform. They are absent from the default branch until that PR merges.

Verified on PR #1 head `0194d71` (this repo only, no sibling checkouts):

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements-quality.txt
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=100 --show-missing
python3 scripts/validate_repository.py
```

Observed: 18 tests passed, the repository scripts remain 100% covered, and `repository contracts valid`. The foundation branch does not import the future runtime package; these files are not on `develop` until the foundation merges.

The foundation design defers runtime APIs, deterministic rating, credits, spend reservation, invoice generation, provider adapters, and a live PostgreSQL integration suite to later reviewed increments.

## How siblings call it

No published HTTP, RPC, or webhook contract exists on the default branch. Siblings must not assume a base URL, authentication scheme, or path.

When the usage-event contract from PR #1 is on the branch you are integrating against, a usage or cost caller emits a JSON document that conforms to `schemas/usage-event.schema.json` (`$id`: `https://schemas.contextualwisdomlab.org/metering-billing/usage-event/v1`). That schema is the published call contract. There is still no ingestion route in this repository that accepts it.

Required fields on that contract:

- `event_id` (UUID)
- `event_contract_version` (integer ≥ 1)
- `source_event_key`
- `source_payload_hash` (`sha256:` + 64 hex characters)
- `tenant_reference`, `billing_account_reference`, `billing_principal_reference` (`urn:cwl:`…)
- `product_code`
- `occurred_at`
- `measurements[]` with `meter_code`, `quantity` (decimal string), `unit_code`, and `quality_code`

Optional attribution on the same contract: `credential_reference`, `cost_center_reference`, `project_reference`, `operation_code`, `recorded_at`. Prompt text, response text, card data, and credential plaintext are rejected (`additionalProperties: false`).

Intended callers named in this repository's foundation documents:

| Sibling | Intended relationship | Published call on default branch |
| --- | --- | --- |
| [contextual-orchestrator](https://github.com/ContextualWisdomLab/contextual-orchestrator) | First commercial usage/cost producer | None. Emit the usage-event schema when that file is on the integration branch. |
| Other CWL usage producers | Same usage-event contract | None. |
| [naruon](https://github.com/ContextualWisdomLab/naruon) | CWL composition hub that may host or compose this product | None. Composition is optional and host-owned. Independent run does not call Naruon. |
| [accounting-information-platform](https://github.com/ContextualWisdomLab/accounting-information-platform) | Consumer of `accounting_journal_proposal`; returns a posting receipt in the specified boundary | None. The proposal schema exists on PR #1; this repo must not mark a proposal `posted`. |

Provider capability manifests use `schemas/provider-capability.schema.json` on the foundation branch. They describe adapter roles and capabilities; they are not a live provider integration.

## Design references

The foundation documents already cite these sources (APA 7th). They inform the contract and accounting-boundary design. This repository does not claim certified IFRS, FinOps, or ISO implementation.

Cloud Native Computing Foundation. (2025). *CloudEvents specification (Version 1.0.2).* https://github.com/cloudevents/spec

FinOps Foundation. (2026). *FinOps Open Cost and Usage Specification (FOCUS), Version 1.4.* https://focus.finops.org/focus-specification/

IFRS Foundation. (2024). *IFRS 15: Revenue from contracts with customers—Supporting material.* https://www.ifrs.org/supporting-implementation/supporting-materials-by-ifrs-standards/ifrs-15/

IFRS Foundation. (2024). *IFRS 18 presentation and disclosure in financial statements.* https://www.ifrs.org/issued-standards/list-of-standards/ifrs-18-presentation-and-disclosure-in-financial-statements/

IFRS Foundation. (2026). *IFRS Accounting Taxonomy 2025 to remain current for 2026 reporting.* https://www.ifrs.org/news-and-events/news/2026/02/ifrs-accounting-taxonomy-2025-to-remain-current-for-2026/

IFRS Foundation. (n.d.). *IAS 7 statement of cash flows.* https://www.ifrs.org/issued-standards/list-of-standards/ias-7-statement-of-cash-flows/

IFRS Foundation. (n.d.). *IAS 21 the effects of changes in foreign exchange rates.* https://www.ifrs.org/issued-standards/list-of-standards/ias-21-the-effects-of-changes-in-foreign-exchange-rates/

International Organization for Standardization. (2026). *ISO 20022-1:2026 financial services—Universal financial industry message scheme—Part 1: Metamodel.* https://www.iso.org/standard/20022-1

PostgreSQL Global Development Group. (2026). *PostgreSQL versioning policy.* https://www.postgresql.org/support/versioning/

CloudEvents envelopes, FOCUS exports, and ISO 20022 bank adapters are deferred in the foundation TRD; they are not present as runtime features.

## Documentation

Buyer and operator material stays in this README. Contributor, agent, and CI procedure is in [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md). Foundation product documents, when present on a branch, are `docs/PRD.md`, `docs/TRD.md`, `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/ACCOUNTING_BOUNDARY.md`, and `docs/adr/`.
