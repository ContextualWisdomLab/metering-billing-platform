# Product and Technical Gap Baseline

Assessment date: 2026-08-20 (Asia/Seoul)

This baseline separates observed repository evidence from the target product
state. It is intentionally a release-gate document, not a claim that the
currently open pull requests are merged or production-ready.

## Evidence boundary

| Evidence | Observed state |
| --- | --- |
| `develop` | Bootstrap commit `17e1408`; the checked-out branch has no implementation before the pull-request chain is merged. |
| Foundation candidate | PR #1, `agent/initial-billing-foundation`, last verified code head `69b3d36`; its exact-head repository-contract check is the first gate. |
| Latest feature candidate | PR #82, `cursor/spend-budget-publish-f556`, last verified head `205a190`; it contains the cumulative Python package, PostgreSQL migrations, HTTP surface, operator-console Storybook, schemas, tests, dependency repairs, outbound URL validation, hollow-resolver hardening, boolean JSON Schema validator support, reusable-workflow reference validation, persistence attribution constraints, catalog URN identity migration, uv project metadata, and matching data-model documentation. |
| Local verification | Foundation tests passed with 100% statement and branch coverage after the contract scanner was corrected to ignore `.venv/`. The cumulative candidate passed 587 Python tests at 100% coverage, optimized-Python resolver tests, 42 operator-console tests, lint, Storybook build, local Semgrep with zero findings, real PostgreSQL migration and constraint smoke checks, and production-dependency audit with zero vulnerabilities. Hosted Checks remain authoritative for the pushed heads. |
| Recent gate repair | The prior PR #82 head `f8d8e79` failed OSV on Storybook/Vite/uuid and Semgrep on two outbound `urllib` calls. Head `adea8cc` contains the dependency upgrades and outbound URL revalidation; head `ac592d9` additionally replaces production `assert` resolver guards with `require_resolved` and adds hollow-success regression tests; head `34e3c2a` adds boolean JSON Schema support and edge tests; head `72003a0` adds reusable-workflow reference validation; head `0faa5c9` adds tenant proposal-reference uniqueness, non-overlapping credential intervals, and matching in-memory validation; head `1fc169d` documents those persistence rules in `DATA_MODEL.md`; head `5c64f9d` pins the project-local uv development boundary; head `205a190` adds catalog URN identity backfill. Its new hosted Checks are pending. |
| Authority documents | `docs/PRD.md`, `docs/TRD.md`, `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/ACCOUNTING_BOUNDARY.md`, `docs/SECURITY.md`, `docs/STORYBOOK.md`, ADRs, and `docs/doctoring/*`. |
| External boundary | Accounting Information Platform, Keyverse, contextual-orchestrator, payment/MoR providers, tax services, and bank/treasury systems are described as replaceable or authoritative external systems; their production integration is not inferred from a local merge. |

## Product contract

The product is a provider-neutral commercial control plane:

```text
product usage
  -> tenant/principal/credential attribution
  -> immutable usage and quality decision
  -> versioned rating
  -> invoice intent and commercial corrections
  -> collection/provider projection
  -> settlement and reconciliation evidence
  -> proposal-only accounting export
  -> Accounting Information Platform posting receipt observation
```

The billing platform owns commercial facts and explainability. It must not
claim statutory posting, replace the accounting system of record, or make a
provider identifier an internal authority. The main buyer outcomes are:

1. A platform operator can explain an amount to its usage evidence.
2. A finance operator can move from invoice intent through collection,
   correction, settlement, and reconciliation without rewriting history.
3. A product service can emit usage without implementing prices, taxes, or
   accounting rules.
4. A customer administrator can inspect spend by product and attribution
   dimension without seeing secrets or crossing tenant boundaries.

## Current implementation matrix

| Capability | Current evidence | Status | Buyer-visible gap |
| --- | --- | --- | --- |
| Contract plane | Draft 2020-12 schemas, exact decimal strings, semantic journal validation, and repository validator | Implemented in the PR chain | Schemas need published versioning and compatibility policy. |
| Usage and rating | `metering_billing/usage_ingestion.py`, `usage_rating.py`, migrations `0002` and `0003`, idempotent replay tests | Implemented as an in-memory service slice | No production PostgreSQL repository or concurrent-ingest acceptance test. |
| Commercial lifecycle | Invoice draft, journal proposal, collection, payment intent/receipt, credit, tax, issue/void, dispute, write-off, unapplied cash, account statement, and spend-budget modules | Broad functional slice in current PR #82 | No production provider execution, reconciliation worker, or operational recovery path. |
| HTTP presentment | `metering_billing/http_app.py` and GET/POST contracts for stored facts | Local stdlib adapter | No deployed server contract, rate limiting, request tracing, OpenAPI publication, or authenticated operator portal. |
| Accounting boundary | Proposal-only statuses, AIS pull/drain, posting-receipt observation, ADRs and traceability | Boundary is explicit | No live Accounting Information Platform connector or end-to-end posting-receipt run. |
| Operator UI | `operator_console`, Storybook, tokenized fixtures, exact-decimal render tests | Presentment prototype | Storybook is not a customer/operator release: no login, browser E2E, accessibility gate, i18n gate, or deployment proof. |
| Security | Tenant API credentials are HMAC-verified and revocable; secrets are not returned from presentment | Local contract behavior | No Keyverse/OIDC trust integration, role/permission model, audit trail, key rotation policy, or production secret-management evidence. |
| Persistence | 36 normalized migration files with tenant-scoped foreign keys, proposal-reference uniqueness, and credential-interval exclusion | SQL design and a local PostgreSQL smoke run exist | CI does not execute migrations against PostgreSQL, test locking/isolation, or verify rollback and upgrade behavior; hot partitioning remains unimplemented. |
| Operations | Commit-pinned GitHub Actions and repository quality checks | Source-quality gate exists | No SLOs, metrics, traces, alerts, dead-letter/runbook flow, backup/restore test, or incident evidence. |
| Ecosystem | Provider-neutral contracts and references to CWL systems | Ports are documented | No verified contextual-orchestrator usage connector, Keyverse integration, or reusable package contract consumed by another CWL repository. |

## Prioritized gaps

Priority is based on whether a buyer can safely run the commercial path, not on
the number of source files already present.

### P0 — release blockers

| ID | Gap | Evidence and acceptance condition |
| --- | --- | --- |
| P0-1 | Authoritative persistence and concurrency | The implementation exposes `MemoryUsageLedger` and the quality workflow only executes Python tests. Add a PostgreSQL repository behind the existing service ports; execute all migrations; prove tenant isolation, idempotency under concurrent retries, transaction boundaries, keyset paging, and lock behavior with a real database. |
| P0-2 | Production identity and authorization | `docs/SECURITY.md` documents tenant API keys and a zero-key bootstrap window, while the Storybook README explicitly has no login wall. Integrate the chosen Keyverse/issuer trust boundary, separate service/operator/customer roles, enforce tenant and resource authorization, and record security-relevant decisions. |
| P0-3 | Durable asynchronous execution | Outbox records and explicit drain/delivery services exist, but a deployable worker, retry/backoff policy, lease/claim semantics, dead-letter handling, and replay observability are not evidenced. Ship one durable worker contract and run it against PostgreSQL and a provider test endpoint. |
| P0-4 | Production HTTP and deployment contract | The current adapter is a stdlib testable surface. Define the supported server process, configuration, readiness/liveness, request limits, timeout policy, idempotency headers, error envelope, OpenAPI contract, and Compose smoke test. |
| P0-5 | Money and correction acceptance | Unit tests cover many exact-decimal scenarios, but the release gate must run realistic end-to-end sequences against the authoritative store: usage → rating → invoice → tax → collection → receipt → credit/void/dispute → proposal → observed posting receipt. Totals and compensating facts must remain explainable after retries and concurrent commands. |

### P1 — buyer adoption blockers

| ID | Gap | Acceptance condition |
| --- | --- | --- |
| P1-1 | Real usage producer integration | Add an import/REST connector contract for contextual-orchestrator that emits canonical usage without prompt/response content, preserves attribution, and proves source-hash/idempotency behavior with a replay fixture from the producer contract. |
| P1-2 | Provider and AIS adapters | Implement capability-scoped adapters with mapping records, signature verification, provider-sticky objects, retryable failures, and a real sandbox or contract-test run for collection, refund, webhook, tax, and AIS posting receipt flows. |
| P1-3 | Customer and operator experience | Turn presentment contracts into an authenticated operator/customer surface with empty/loading/error states, actionable next-step copy, keyboard and screen-reader checks, locale/number/date consistency, and browser-clicked E2E coverage. |
| P1-4 | Observability and supportability | Emit structured audit events, correlation IDs, metrics for ingest/rating/collection/outbox lag, traces across tenant and provider boundaries, alerts, dashboards, and runbooks without storing secrets or prompt/response text. |
| P1-5 | Migration and scale proof | Add Compose PostgreSQL smoke tests, upgrade/rollback checks, backup/restore evidence, retention policy, and a bounded partitioning strategy. Partition only after measuring access patterns; hot tenant/account keys must not serialize the entire workload. |
| P1-6 | Public integration surface | Publish API/OpenAPI and package compatibility rules, then consume the same contract from at least one authorized CWL repository. Record the actual merged commit and consumer test; do not count a planned integration as evidence. |

### P2 — enterprise readiness gaps

| ID | Gap | Acceptance condition |
| --- | --- | --- |
| P2-1 | Compliance evidence | Map SOC 2 CC controls and CSAP expectations to executable controls, owners, evidence retention, access reviews, change approval, vulnerability response, and disaster recovery tests. The standards list alone is insufficient. |
| P2-2 | Tax and internationalization | The current slice documents flat tax and limited currency rules. Add versioned jurisdiction, nexus, exemption, rounding, FX-source, effective-date, and statutory-document boundaries or explicitly limit the supported market in the commercial plan. |
| P2-3 | Cost and usage export | Add a FOCUS-compatible projection and reconciliation report without replacing the normalized operational model. Prove that export totals tie back to rated usage, invoices, credits, and settlements. |
| P2-4 | Design source of truth | Storybook tokens are present, but no Figma file ID is recorded in an ADR. If the operator/customer UI remains a product commitment, create the Figma source and record its file ID and token ownership in a new ADR before expanding the component set. |
| P2-5 | Quality scope | CI currently measures Python `scripts` and `metering_billing`; it does not prove JavaScript lint/test/Storybook build, browser interaction, accessibility, i18n, or design-token coverage. Add those gates or keep the console explicitly prototype-only. |
| P2-6 | Privacy-preserving operations | Do not disable controls to expose PII. Define field-level encryption/tokenization, scoped reveal with authorization, immutable access audit, retention/deletion rules, and redacted telemetry so finance workflows remain usable while obligations remain enforceable. |

Rust, GPU, and multithreaded numerical kernels are not a current gap for this
commercial ledger: the present bottleneck is durable transactional state and
integration evidence, not mathematical computation. Revisit a Rust boundary
only after a profile identifies a billing-critical hot path or a trust boundary
that the existing implementation cannot safely satisfy.

## Open pull-request inventory

The following inventory was obtained from the repository's open PR list on
2026-08-20 after removing superseded cumulative snapshots. Every item remains
an open candidate until its current head has a review, terminal Checks, and a
recorded merge result. PR #3 targets the foundation branch; PR #92 stacks on
PR #82; the other listed PRs target `develop`.

| PRs | Scope |
| --- | --- |
| #1 | Establish provider-neutral billing foundation |
| #3 | Expand operator README, ADRs, APA references, and validation documentation on the foundation branch |
| #4 | Buyer/operator README and contributor documentation |
| #82 | Cumulative commercial lifecycle and spend-budget publication, including current security hardening |
| #92 | Draft product and technical gap baseline stacked on the current PR #82 head |

PRs #5–#81 are closed as superseded snapshots. Their code ancestry is
contained in #82 except for #7's resolver-hardening commit; that behavior was
reapplied directly to #82 at `ac592d9` with optimized-Python regression tests;
the current #82 head is `205a190`.
PR #6 was separately superseded by #7 before the cumulative cleanup. The
closure reason is recorded on each PR; no closed snapshot is treated as a
merge or production evidence.

The grouped entries above preserve the current PR scope while keeping the
merge loop reviewable. PR #92 is the current detailed baseline candidate and
remains draft until its stacked base and release topology are settled. The
authoritative titles, head SHAs, base SHAs, review states, and Checks are the
GitHub records at review time; this file must be updated whenever that
inventory or the merge order changes.

## Merge and development loop

For each candidate: inspect the current head and changed files, resolve valid
review findings, rerun the repository and domain checks, re-fetch the exact
head, merge only with the required review and terminal Checks, verify the merge
SHA, then continue with the next dependency-aware candidate. While GitHub
review or Checks are pending, run safe local database, contract, security, and
browser diagnostics; do not manufacture a passing result or rerun unchanged
external checks.

## Completion gate

This baseline is complete only when the P0 conditions have current runtime
evidence, the P1 adoption path has a real consumer and provider/AIS proof, the
P2 controls are either implemented or explicitly excluded from the supported
market, and every open PR is merged or closed with its reason recorded. A
green unit-test job, a Storybook fixture, or a merged PR by itself is not proof
of commercial readiness.

## Standards and research traceability

The normative and research references remain centralized in
`docs/doctoring/REFERENCES.md` and mapped to engineering decisions in
`docs/doctoring/STANDARD_TRACEABILITY.md`. The current baseline relies in
particular on CloudEvents, FOCUS, IFRS 15/18, IAS 7/21, ISO 20022, PCI DSS,
NIST SP 800-63B, OWASP API guidance, RFC 2104, Google AIP-158, TM Forum
TMF620, ISO 4217, and PostgreSQL versioning guidance already listed there.
New provider, tax, identity, or UI decisions must add an APA 7th reference
and a traceability row before the implementation is treated as a release
contract.
