# Product and Technical Gap Baseline

Assessment date: 2026-08-20 (Asia/Seoul)

This baseline separates observed repository evidence from the target product
state. It is intentionally a release-gate document, not a claim that the
currently open pull requests are merged or production-ready.

## Evidence boundary

| Evidence | Observed state |
| --- | --- |
| `develop` | Bootstrap commit `17e1408`; the checked-out branch has no implementation before the pull-request chain is merged. |
| Foundation candidate | PR #1, `agent/initial-billing-foundation`; current head is tracked in GitHub and its exact-head repository-contract check is the first gate. |
| Latest feature candidate | PR #82, `cursor/spend-budget-publish-f556`; it contains the cumulative Python package, PostgreSQL migrations, HTTP surface, operator-console Storybook, schemas, and tests. |
| Local verification | Foundation tests passed with 100% statement and branch coverage after the contract scanner was corrected to ignore `.venv/`. Hosted checks remain authoritative for the pushed head. |
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
| Commercial lifecycle | Invoice draft, journal proposal, collection, payment intent/receipt, credit, tax, issue/void, dispute, write-off, unapplied cash, and account statement modules | Broad functional slice in PRs #8-#82 | No production provider execution, reconciliation worker, or operational recovery path. |
| HTTP presentment | `metering_billing/http_app.py` and GET/POST contracts for stored facts | Local stdlib adapter | No deployed server contract, rate limiting, request tracing, OpenAPI publication, or authenticated operator portal. |
| Accounting boundary | Proposal-only statuses, AIS pull/drain, posting-receipt observation, ADRs and traceability | Boundary is explicit | No live Accounting Information Platform connector or end-to-end posting-receipt run. |
| Operator UI | `operator_console`, Storybook, tokenized fixtures, exact-decimal render tests | Presentment prototype | Storybook is not a customer/operator release: no login, browser E2E, accessibility gate, i18n gate, or deployment proof. |
| Security | Tenant API credentials are HMAC-verified and revocable; secrets are not returned from presentment | Local contract behavior | No Keyverse/OIDC trust integration, role/permission model, audit trail, key rotation policy, or production secret-management evidence. |
| Persistence | 35 normalized migration files with tenant-scoped foreign keys | SQL design exists | CI does not execute migrations against PostgreSQL, test locking/isolation, or verify rollback and upgrade behavior. |
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
2026-08-20. Every item remains an open candidate until its current head has a
review, terminal Checks, and a recorded merge result. PR #3 targets the
foundation branch; the other listed PRs target `develop`.

| PRs | Scope |
| --- | --- |
| #1 | Establish provider-neutral billing foundation |
| #3–#4 | Operator ADR/README documentation and buyer/operator README |
| #5 | Immutable usage ingestion and idempotent deduplication |
| #6–#7 | Deterministic time-windowed usage rating |
| #8 | Invoice draft from rated usage |
| #9 | Journal proposal from invoice draft |
| #10 | Collection case and dunning from invoice draft |
| #11 | Payment intent from collection case |
| #12 | Payment receipt and settlement from payment intent |
| #13 | Cash journal proposal from payment receipt |
| #14 | Standard-library HTTP accept surface |
| #15 | Journal proposal query HTTP for AIS pull |
| #16 | AIS posting receipt observation |
| #17 | Credit adjustment from invoice draft |
| #18 | Versioned rate-card catalog |
| #19 | Tax assessment on invoice draft |
| #20 | Tax-payable unwind on credit adjustment |
| #21 | Invoice draft presentment HTTP |
| #22 | Tenant API credentials for HTTP |
| #23 | Operator presentment Storybook |
| #24 | Commercial webhook outbox |
| #25 | AIS outbox drain |
| #26 | Collection-case presentment |
| #27 | Payment-intent HTTP |
| #28 | Payment-receipt HTTP |
| #29 | Cash journal on receipt accept |
| #30 | Credit-adjustment HTTP presentment |
| #31 | Rate-card HTTP presentment |
| #32 | Usage-event HTTP presentment |
| #33 | Rating-run HTTP presentment |
| #34 | Tax-assessment HTTP presentment |
| #35 | Posting-receipt observation HTTP presentment |
| #36 | Webhook-delivery HTTP presentment |
| #37 | Tenant API credential presentment |
| #38 | Webhook-subscription presentment |
| #39 | Dunning-event presentment |
| #40 | Webhook-outbox-event presentment |
| #41 | Issued invoice from invoice draft |
| #42 | Invoice-issued webhook |
| #43 | Issued credit note from credit adjustment |
| #44 | Credit-note-issued webhook |
| #45 | Credit-note application to collection case |
| #46 | Collection-case settlement when zero |
| #47 | Collection-settled webhook |
| #48 | Credit-note-applied webhook |
| #49 | Collection write-off |
| #50 | Write-off-recorded webhook |
| #51 | Write-off journal proposal |
| #52 | Credit journal proposal |
| #53 | Collection aging presentment |
| #54 | Unapplied cash from payment receipt |
| #55 | Unapplied-cash application |
| #56 | Unapplied-cash-applied webhook |
| #57 | Unapplied-cash refund |
| #58 | Refund-recorded webhook |
| #59 | Refund journal proposal |
| #60 | Unapplied-cash journal proposal |
| #61 | Unapplied-cash application journal |
| #62 | Billing-account statement presentment |
| #63 | Commercial issued-invoice void |
| #64 | Invoice-voided webhook |
| #65 | Void journal proposal |
| #66 | Collection-dispute hold |
| #67 | Collection-dispute release |
| #68 | Dispute-held webhook |
| #69 | Dispute-released webhook |
| #70 | Issued-invoice tax-assessment presentment |
| #71 | Issued-credit-note tax-assessment presentment |
| #72 | Issued-credit-note void |
| #73 | Credit-note-voided webhook |
| #74 | Credit-note-void journal proposal |
| #75 | Account-statement void totals |
| #76 | Operator account-statement Storybook |
| #77 | Rated spend by product |
| #78 | Rated spend grouped by project |
| #79 | Rated spend grouped by credential |
| #80 | Rated spend grouped by principal |
| #81 | Rated spend grouped by cost center |
| #82 | Commercial spend budget publication |

The grouped entries above preserve the current PR scope while keeping the
merge loop reviewable. The authoritative titles, head SHAs, base SHAs, review
states, and Checks are the GitHub records at review time; this file must be
updated whenever that inventory or the merge order changes.

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
