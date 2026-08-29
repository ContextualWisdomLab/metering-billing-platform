# ADR 0129: Operational runbooks and validation receipts

- **Status:** Accepted for the #91 operational-evidence slice
- **Date:** 2026-08-29
- **Decision owners:** Billing on-call, security on-call, finance operations

## Context

Issue #91 requires executable or tabletop-validated support procedures for
commercial, provider, database, security, and tenant-lifecycle failures. The
repository owns immutable commercial facts but does not own provider customer
records, card data, statutory books, or a production secret manager. A runbook
must therefore identify the authority boundary and preserve evidence without
turning an operator document into an unsafe repair script.

## Decision

Keep one indexed set of scenario-specific Markdown runbooks under
`docs/operations/runbooks/`. Every procedure declares owner, severity and
escalation, customer communication, recovery objective, evidence preservation,
detection, containment, diagnosis, recovery, validation receipt, and exit/RCA.
`scripts/validate_repository.py` checks those sections on every exact-head
validation. A receipt records the deployed SHA and measured outcomes; a green
document validator is not a claim that a live outage, backup restore, provider,
or RPO/RTO rehearsal has succeeded.

The procedures use the platform’s existing liveness/readiness endpoints,
append-only replay rules, migration runner, and tenant-safe presentment. Any
state-changing action remains behind its normal authorization and change record.
Secrets and protected content are represented only by redacted identifiers or
checksums.

## Consequences

- Support has a common first response and escalation vocabulary.
- Missing runbook sections fail repository validation before release.
- Live rehearsal receipts, measured capacity, and production RPO/RTO remain
  required release evidence and cannot be satisfied by documentation alone.

## Research basis

The incident procedures follow the lifecycle and improvement emphasis of NIST
SP 800-61 Rev. 3. Recovery and exercise language follows the contingency
planning process in NIST SP 800-34 Rev. 1. See the APA-formatted entries in
`docs/doctoring/REFERENCES.md` and the mapping in
`docs/doctoring/STANDARD_TRACEABILITY.md`.
