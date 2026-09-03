# Metering Billing Platform

Metering Billing Platform is ContextualWisdomLab's provider-neutral commercial control plane for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

## Product responsibility

This repository owns commercial usage and billing truth: immutable usage evidence, metering and rating, commercial budget and entitlement decisions, invoice intent and issued commercial invoice facts, payment and collection projections, and reconciliation evidence. Statutory accounting, legal books, posted journals, fiscal close, consolidation, and financial statements remain the responsibility of Accounting Information Platform.

## Start here

- [Repository README](../README.md) — product authority, current runnable surface, and operating examples.
- [Product and technical gap baseline](product-technical-gap-baseline.md) — current commercialization gaps and evidence.
- [Architecture decisions](adr/) — accepted product and operational decisions.
- [Operations](operations/) — bounded runbooks and operational evidence.
- [GitHub Releases](https://github.com/ContextualWisdomLab/metering-billing-platform/releases) — immutable release artifacts when available.
- [Ask DeepWiki](https://deepwiki.com/ContextualWisdomLab/metering-billing-platform) — repository-aware navigation and questions.

## Architecture boundary

CWL products publish canonical usage events into the commercial control plane. Metering Billing Platform preserves tenant-scoped commercial facts and can produce accounting journal proposals, but those proposals are not statutory postings. Accounting Information Platform remains authoritative for posted journals and financial statements.

Provider-specific integrations remain projections behind explicit capability and evidence boundaries. Commercial facts should not be reconstructed by reading another product's application tables.

## Security, operations, and release evidence

Tenant isolation, exact-decimal conservation, immutable evidence, credential handling, recovery rehearsal, dependency provenance, and deterministic release evidence are release-readiness concerns. A branch-only implementation, local test result, or draft documentation claim is not equivalent to protected integration, a published immutable release, or live production evidence.

## Publication status

This file is a GitHub Pages source prerequisite, not proof that Pages is live. Publication is complete only after the reviewed source reaches the protected default branch, the organization-owned repository metadata reconciler applies the intended Pages configuration, deployment succeeds, and the public HTTPS content is re-read successfully.
