# ADR 0127: Bounded AIS Response Admission

**Status:** Proposed
**Date:** 2026-10-04

## Context

Issue #88 identifies three unbounded AIS response reads: posting-receipt
lookup, outbox list, and outbox publish. A hostile or misconfigured response
could be fully retained before contract validation. HTTPError handles were
also not explicitly closed by these consumers.

The local consumer must contain immediate resource risk without copying
mutable EgressWeave source or claiming ownership of reusable outbound policy.
The existing scheduler PR #155 does not modify this response client. The
existing PostgreSQL client replacement remains owned by PR #180.

## Decision

Use one fixed local admission ceiling, `AIS_RESPONSE_MAX_BYTES = 1_048_576`
(one MiB), for all three success-response consumers. Read at most `limit + 1`
bytes, reject overflow as the existing `AisTransportError("transport_failure")`,
and do not parse or return those excess bytes. Content-Length is not trusted
as a size limit or required, but the stdlib HTTPResponse's parsed remaining
length must be fully received when present. Capture that length before reading
and reject a shorter body even when it is valid JSON. Normalize HTTPException
(including IncompleteRead from chunked framing) into stable transport failure.
The response context owns closure, including overflow, read failure, and
unexpected-status paths. Injected openers must return context-managed blocking
byte readers with HTTPResponse-compatible full-read/EOF semantics; non-byte
results reject. Nonblocking short-read streams are not this injection contract.

Do not read HTTP error bodies at all. Explicitly close the HTTPError handle
before returning the established 403/404 mapping or rejecting other statuses.
If that close raises OSError or ValueError, return a stable transport denial
rather than leaking a cleanup exception or claiming the mapping succeeded.
Never return an upstream error body. Do not retry cleanup or provider actions.

The ceiling is an explicit proposed local operational bound, not a measured
maximum AIS document size or negotiated upstream SLA. Responses exactly at the
ceiling remain admissible; responses larger than it require an upstream page
or document change or an independently reviewed ceiling change. Existing
opaque identity/tenant/payload semantics and proposal-only accounting remain.

## Verification

Test-first controls reproduce unbounded reads on all three routes and missing
HTTP error cleanup for 403, 404, and 500. Read-size tracking proves a maximum
of `limit + 1` delivered bytes with context closure. Exact-ceiling valid JSON,
read failures, success/error cleanup failures, and normal 403/404 controls are
paired. A real default-opener loopback fixture omits Content-Length, serves
oversized otherwise valid JSON, then serves a normal page to prove recovery.
Owned server threads are joined after shutdown.

Independent review rejected the initial bounded-read candidate because a short
Content-Length body containing valid JSON could be accepted and chunked
IncompleteRead escaped the stable error envelope. Keep its 713-test/100%
receipt historical, not repair acceptance. Real HTTP regressions now pair both
truncation denials on all three routes with complete length/chunked positives.
A real valid receipt remains unstored on either framing failure and stores once
with duplicate replay after complete responses. Non-byte injected reads reject.
The corrected whole candidate requires new canonical gates and review.

The complete local union still requires canonical PostgreSQL/full tests,
100% production statement/branch coverage, repository contracts, and fresh
independent review. Local HTTP fixtures are not live AIS evidence.

## Consequences and remaining gaps

All three consumers now enforce the same byte admission policy. Existing
fake response readers accept the standard read(size) argument. No dependency,
migration, accounting post, identity-bootstrap change, or scheduler is added.

This does not resolve all issue #88 acceptance. The byte cap is not a global
streaming/decompression policy or an end-to-end deadline. Redirect prohibition,
DNS rebinding, destination/proxy/TLS governance, production secrets/identity,
and released EgressWeave adapter/provenance remain separate open work. An
oversized publish response may follow an already-performed remote publish;
a local transport denial does not prove rollback or authorize duplicate sends.
