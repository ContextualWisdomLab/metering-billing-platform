# ADR 0126: Strict AIS Outbox Field Types

**Status:** Proposed
**Date:** 2026-10-03

## Context

Issue #88's owner-path comment identifies coercion of external AIS JSON by
`_parse_outbox_page`. The existing drain matches opaque references and publishes
an AIS event after storing its observation. Converting an external boolean,
number, null, or container with `str(...)` manufactures apparent identifiers
rather than admitting the source contract. A valid first row must not be acted
on when a later row in the same fetched page is malformed.

PR #155 owns the optional scheduler, not this parser repair. No scheduler,
identity-bootstrap, PostgreSQL client, or provider work is adopted here.

## Decision

Require all six existing outbox row fields to be nonempty strings:
`outbox_event_id`, `event_type_code`, `aggregate_reference`, `payload_reference`,
`payload_hash`, and `created_at`. Missing or invalid values raise the existing
stable `AisTransportError("transport_failure")` without reflecting raw values.
Construct `AisOutboxEvent` only after every field passes. Admit the whole page
before the drain receives it, so no row in a rejected page triggers receipt
lookup, observation persistence, or publish.

Keep references opaque and byte-exact. Do not invent AIS identifier/hash/time
syntax, take over AIS schema authority, or parse payload URNs. Existing extra
fields and legacy `items`/`cursor` remain ignored. `next_cursor` behavior is
unchanged. Earlier pages already processed are not rolled back by a later
invalid page. Billing proposals remain `validated`, never posted.

## Verification

A test-first negative matrix exercises all six fields against null, boolean,
integer, float, array, object, and empty string (42 failing controls on the
original parser). A real loopback AIS fixture serves a valid row followed by a
malformed row, verifies zero receipt/publish calls and no observation, then
serves a valid-only page and verifies successful observation/publish with
unchanged Billing proposal status. Existing missing-field and extra-field tests
remain applicable.

The complete composed candidate must satisfy the repository's 100% production
statement/branch coverage gate and independent local review before completion.
Local fixtures are not live AIS integration or hosted approval.

## Consequences

Malformed source fields no longer become apparently valid strings. Valid AIS
pages retain their existing contracts and behavior. This consumer input
containment does not resolve all issue #88 acceptance. Subsequent local response-size
containment and HTTP framing checks are described in ADR
`0127-bounded-ais-response-admission`. Redirects, DNS/egress policy, production
identity/secrets, and supply-chain evidence remain open. Reusable egress policy
belongs to its existing ecosystem owner; this parser repair introduces no new
dependency or transport policy.
