# Metering Billing Platform

CWL's intended commercial system of record for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

This repository is still a **foundation / bootstrap**. The default branch does not ship a runtime, HTTP API, SDK, or payment-provider adapter. Product contracts and the first offline validator live on draft [pull request #1](https://github.com/ContextualWisdomLab/metering-billing-platform/pull/1) (`agent/initial-billing-foundation`). Treat that work as a reviewed proposal, not as shipped behavior.

Naruon is the CWL composition hub and may compose this product later. That is an intended host relationship, not a boot dependency. This platform is specified to run independently and does not require a Naruon checkout, process, or configuration.

## What the product is defined to do

The draft foundation PRD states the outcome: organizations can attribute AI-platform and CWL-product usage to a billing account, principal, credential, project, and cost center; apply versioned commercial rules; control spend; explain charges; and project those results to replaceable collection providers.

Primary users named in that PRD:

- Platform operators configure meters, prices, budgets, provider accounts, and reconciliation.
- Finance operations review invoice intent, collections, refunds, settlement, and accounting exports.
- Customer administrators inspect usage and spend by product, project, principal, and credential.
- Product services emit usage without implementing price or accounting logic.

Required properties already written into the PRD (not yet implemented as a service):

1. At-least-once event delivery produces at-most-once monetary effects.
2. Estimated usage is not automatically billable.
3. Price, contract, and meter changes do not rewrite historical rating outcomes.
4. Provider customer and subscription identifiers stay behind mapping records.
5. Payment, refund, dispute, and settlement facts remain provider-sticky after creation.
6. Every invoice line is explainable down to its usage evidence.
7. Accounting exports are proposals and cannot claim statutory posting.

The first commercial vertical named in the PRD is `contextual-orchestrator` usage, then billability, deterministic aggregation, invoice intent, manual enterprise invoice or Lemon Squeezy projection, payment and settlement evidence, reconciliation, and an accounting journal proposal.

## Authority boundary

This repository is defined to own commercial usage and billing truth. It does **not** own the statutory chart of accounts, legal books, posted journals, fiscal close, consolidation, or financial statements. Those belong to [accounting-information-platform](https://github.com/ContextualWisdomLab/accounting-information-platform).

```text
CWL products
  -> canonical usage events
  -> Metering Billing Platform
  -> invoice and settlement facts
  -> accounting journal proposals
  -> Accounting Information Platform
  -> posted journals and financial statements
```

Identity authentication remains with Keyverse or the credential issuer. Credentials are attribution dimensions, not customers. Payment processors, merchant-of-record services, and gateways are replaceable capability adapters; their object IDs are not internal primary keys.

## Current contents

| Location | What exists |
| --- | --- |
| Default branch (`agent/initial-foundation`, also `main`) | This buyer/operator README and contributor operation notes. No application code. |
| Draft [PR #1](https://github.com/ContextualWisdomLab/metering-billing-platform/pull/1) | PRD, TRD, architecture, data model, accounting boundary, two accepted ADRs, APA 7th reference list, JSON Schema Draft 2020-12 contracts, PostgreSQL 18 core migration, offline repository validator, exact-head CI. |
| Not present in this repository | A bindable HTTP server, OpenAPI/AsyncAPI, live PostgreSQL suite, rating engine, invoice API, provider webhook receiver, or published sibling SDK. |

## Independent run

This repository runs without Naruon and without any sibling checkout. There is no compose file, environment template, or process to start on the default branch.

```bash
git clone https://github.com/ContextualWisdomLab/metering-billing-platform.git
cd metering-billing-platform
```

That clone is the whole local product surface on the default branch: this README and `docs/`.

The foundation PR defines additional offline commands. They contact no external service, do not start a billing API, and do not require Naruon, contextual-orchestrator, or accounting-information-platform. They are absent from the default branch until that PR merges.

Verified on PR #1 head `317f11a` (this repo only, no sibling checkouts):

```bash
python3 scripts/validate_repository.py
# repository contracts valid
```

The same head also documents this test block:

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements-quality.txt
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=100 --show-missing
```

Those commands are defined, but they do not currently run on `317f11a`: `tests/test_repository_contracts.py` fails to import with `SyntaxError: '(' was never closed` at the escaped JSON Pointer assertion. Do not treat the test block as passing local verification until that head is repaired.

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
