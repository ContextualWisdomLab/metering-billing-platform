# Metering Billing Platform

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/ContextualWisdomLab/metering-billing-platform)

**Provider-neutral commercial metering and billing control plane for explainable usage, rating, invoicing, collections, payments, credits, and reconciliation.**

Metering Billing Platform turns canonical product-usage evidence into durable commercial facts. Product teams can emit usage without each rebuilding pricing, billing-account attribution, collections, provider reconciliation, or accounting-export logic. Finance and platform operators retain a traceable path from a charge back to the usage and commercial rule that produced it.

## Why it exists

| Job | What Metering Billing Platform provides |
| --- | --- |
| Attribute usage | Tenant-, billing-account-, project-, principal-, credential-, and cost-center-scoped commercial evidence. |
| Price consistently | Versioned rate cards and deterministic rating that do not rewrite historical outcomes when prices change. |
| Control spend | Budget and entitlement decisions over durable commercial facts. |
| Explain invoices | Invoice intent and issued commercial facts traceable to rated usage evidence. |
| Collect and reconcile | Provider-neutral payment, receipt, credit, collection, settlement, and reconciliation evidence. |
| Hand off to accounting | Balanced accounting journal **proposals**; statutory posting remains outside this product. |

## Product boundary

Metering Billing Platform owns **commercial usage and billing truth**. It does not own statutory charts of accounts, legal books, posted journals, fiscal close, consolidation, or financial statements. Those remain the responsibility of the Accounting Information Platform.

```text
CWL product or service
        │ canonical usage evidence
        ▼
Metering Billing Platform
  attribution → rating → invoice → collection/payment/credit → reconciliation
        │ accounting journal proposal
        ▼
Accounting Information Platform
        │ authorized statutory posting
        ▼
Posted journals and financial statements
```

Provider-specific payment and collection systems are replaceable projections behind explicit mappings and evidence boundaries. A provider identifier never becomes the platform's canonical customer identity simply because one adapter uses it.

## Commercial invariants

The current product contract is intentionally conservative:

- at-least-once delivery must produce at-most-once monetary effects;
- estimated or reconstructed usage is not automatically billable;
- published prices and historical rating outcomes are immutable rather than rewritten in place;
- invoice lines remain explainable down to their usage evidence;
- payment, refund, dispute, and settlement facts retain provider provenance after creation; and
- accounting exports are proposals and never imply that a statutory journal was posted.

See [`docs/PRD.md`](docs/PRD.md) for the complete requirement and acceptance contract.

## Current maturity

The repository contains a substantial PostgreSQL-backed commercial vertical, HTTP adapters, machine-readable contracts, deterministic validation, operator-console Storybook evidence, and operations/runbook material. Source metadata currently identifies the project as `0.2.0`; that metadata is **not** release, deployment, customer, certification, or production-readiness evidence. Use [GitHub Releases](https://github.com/ContextualWisdomLab/metering-billing-platform/releases) as the immutable release-discovery surface.

### Commercial dependency blocker

The current source tree declares a reachable `psycopg[binary]>=3.2,<4` PostgreSQL runtime path. That GPL-family/copyleft inbound license is outside the ContextualWisdomLab commercial-license intake policy, even though this repository's original source is Apache-2.0. Issue **#176** owns replacement or removal while preserving the existing PostgreSQL transaction, migration, concurrency, idempotency, error, and verification contracts.

Until #176 is integrated and the resulting dependency graph is revalidated, **do not treat the current source tree or its Compose/`uv` dependency installation as an approved commercial distribution path**. The root Apache-2.0 grant does not relicense third-party dependencies.

## Architecture and evidence model

Commercial decisions are evidence-bound rather than reconstructed from mutable provider state. The platform uses versioned contracts and tenant-scoped persistence for usage, rating, invoice, collection, payment, credit, tax, budget, provider-projection, webhook, reconciliation, and accounting-proposal facts.

Key design material:

- [Architecture](docs/ARCHITECTURE.md) — bounded contexts, integration responsibilities, and trust boundaries.
- [Accounting boundary](docs/ACCOUNTING_BOUNDARY.md) — where billing authority ends and statutory accounting begins.
- [Data model](docs/DATA_MODEL.md) — normalized persistence and durable identity model.
- [Technical requirements](docs/TRD.md) — implementation and quality requirements.
- [Product and technical gap baseline](docs/product-technical-gap-baseline.md) — current commercialization gaps and acceptance evidence.

## Security and privacy

The product is tenant-scoped and evidence-oriented. Commercial HTTP paths reject payment-card PAN/CVC and provider secrets where those values are outside the command contract. Provider credentials, tenant credentials, webhook secrets, authorization, audit evidence, and production key custody are separate security responsibilities rather than fields to leak into routine product payloads or logs.

Read [`docs/SECURITY.md`](docs/SECURITY.md) for the code-current security boundary. Passing source checks does not establish SOC 2, CSAP, payment-industry certification, live tenant isolation, or production key-management evidence.

## Operations

Operator procedures are indexed in [`docs/operations/runbooks.md`](docs/operations/runbooks.md). They cover bounded incident and finance-operation scenarios and are designed to fail closed when required evidence is unavailable. Tabletop procedures are not a substitute for live backup/restore, failover, chaos, measured RPO/RTO, or production recovery evidence.

The public documentation landing source is [`docs/index.md`](docs/index.md). Its presence does not mean GitHub Pages has been published; publication requires protected integration, organization-owned settings/deployment reconciliation, and a live HTTPS content check.

## Verification

The repository's validation contract is maintained separately from customer-facing onboarding. Maintainers should start with [`docs/doctoring/VALIDATION.md`](docs/doctoring/VALIDATION.md) and [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md). Exact-head CI, branch coverage, PostgreSQL integration, security/dependency evidence, review state, and protected-branch governance must all be evaluated on the unchanged candidate revision.

Because #176 currently blocks the approved dependency path, this README deliberately does not present an install command that would import the disallowed runtime dependency merely to make a quickstart look complete.

## Documentation

- [Product requirements](docs/PRD.md)
- [Technical requirements](docs/TRD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Data model](docs/DATA_MODEL.md)
- [Security](docs/SECURITY.md)
- [Storybook inventory and interaction evidence](docs/STORYBOOK.md)
- [Architecture decisions](docs/adr/)
- [Operations and runbooks](docs/operations/)
- [Product and technical gap baseline](docs/product-technical-gap-baseline.md)
- [Contributor guidance](docs/CONTRIBUTING.md)
- [Public documentation landing](docs/index.md)

## Contributing and support

Changes should preserve the billing-versus-accounting boundary, exact-decimal money semantics, tenant isolation, immutable historical commercial facts, replay safety, and evidence traceability. Use [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) for repository procedure. Operational responders should use the owner-scoped runbooks rather than infer recovery actions from implementation detail in this landing page.

## License

ContextualWisdomLab original source in this repository is licensed under the [Apache License 2.0](LICENSE). Third-party packages, provider APIs, standards, datasets, generated assets, and external services retain their own terms and must satisfy the ContextualWisdomLab commercial-license intake policy before incorporation or distribution.
