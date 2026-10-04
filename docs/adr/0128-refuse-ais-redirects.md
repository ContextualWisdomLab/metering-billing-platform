# ADR 0128: Refuse AIS redirects before second-hop I/O

## Status

Accepted for the local candidate. This decision does not mark issue #88 complete
or establish GitHub approval, protected-branch shipment, or deployed acceptance.

## Context

The default AIS opener checked the initial operator-configured URL and then
used stdlib `urlopen`, which follows redirects. Actual loopback HTTP showed
that receipt and outbox GETs followed 301/302/303/307/308, and publish POSTs
followed 301/302/303 after rewriting the method to GET. Tenant headers reached
the redirected target. Same-origin and different-port authorities both showed
this behavior. A valid redirected receipt could persist an observation.
The bounded response reader in ADR 0127 is not a redirect policy.

Issue #88 permits redirect prohibition or destination revalidation. Reusable
destination, DNS, proxy, TLS and resource policy remains externally owned.
Billing must not import mutable or unreleased EgressWeave source to resolve
this consumer-specific failure. Webhook PR #144 and scheduler PR #155 are
separate changes and are not superseded by this ADR.

## Decision

Keep the existing initial-URL check and use a private stdlib redirect handler
in the default AIS opener. Its `redirect_request` raises `HTTPError` with the
original response handle, rather than returning a new request. Refuse all
standard redirect statuses, including same-origin redirects. Do not allowlist
redirect targets or broaden allowed schemes or hosts.

Each existing receipt, outbox-list and publish consumer explicitly closes the
unread error response and returns stable `transport_failure`. Cleanup failure
also denies. No redirect body, Location header or remote error message is
projected into the commercial result. Injected openers remain trusted transport
adapter seams; they must implement equivalent redirect refusal as well as the
blocking byte-read contract of ADR 0127.

Do not change tenant, idempotency, observation, source-proposal or accounting
contracts. A redirected valid receipt writes no observation. A later complete
direct response stores one observation and duplicate replay preserves its ID;
Billing `proposal_status` remains `validated`.

## Verification

A real default-opener wire matrix pairs three routes, five statuses and
same-origin/different-port targets. Every denial requires zero requests at the
target, not just a rejected final response. Direct success controls preserve
all three routes. A service-level matrix serves an otherwise valid known AIS
receipt through redirects and requires rejected outcomes, zero observations,
and zero target requests, then direct acceptance and same-ID duplicate replay.
The fixtures shut down their owned listeners and join every server thread.

The initial in-repository matrix failed in 26 subtests before the opener change.
An isolated exact predecessor candidate also exercises the final service test.
Its first observer accessed a missing rejection reason after acceptance and
therefore errored; that evidence is retained, not counted as intended RED.
Checking the rejected outcome first yields assertion-only failures exposing
redirect acceptance and its already-stored observation. Gate counts and hashes
are recorded in task-owned scratch receipts, not inferred from prior candidates.
The full union requires fresh canonical PostgreSQL tests, 100% production
statement/branch coverage, repository validation and independent source review.

## Consequences and limits

An AIS deployment that redirects these endpoints must configure the actual
endpoint directly; Billing will not follow even an otherwise legitimate redirect.
Existing direct 200 and 403/404 behavior, size ceilings, framing checks and error
cleanup remain intact. No runtime dependency or database schema is added.

This narrowly closes automatic redirect following by the default AIS consumer.
It does not claim initial-target private-address denial, DNS rebinding defense,
proxy isolation, certificate governance, end-to-end deadlines, reusable egress
integration, production identity, compliance or complete issue #88 acceptance.
