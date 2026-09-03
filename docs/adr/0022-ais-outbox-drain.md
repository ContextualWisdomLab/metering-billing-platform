# ADR 0022: AIS Outbox Drain for Posting Receipts

**Status:** Accepted

## Context

AIS Draft #2 (head `377e6b5`, ADR 0017) publishes unpublished commercial facts on `GET /outbox-events`.  Billing already stores posting-receipt observations from an explicit `GET /posting-receipts?idempotency_key=` pull (ADR 0013).  Operators need one drain that consumes the AIS outbox, then stores the receipt observation, so AIS is polled for receipts only when the unpublished set is non-empty (Fielding et al., 2022).

AIS pins `payload_reference` for `event_type_code=posting_receipt`.  Billing must not invent a URN parser.  The idempotency key is not in that URN.

## Decision

- Expose `AisOutboxDrainService.drain_ais_outbox(tenant_reference)` and the optional `AisOutboxScheduler` worker loop. `POST /v1/ais-outbox-drains` remains the explicit operator trigger.
- `GET {AIS_BASE_URL}/outbox-events?event_type_code=posting_receipt` with `X-CWL-Tenant-Reference`.  Read body `outbox_events` and `next_cursor` only.  Never read body `items` or body `cursor`.  A missing `outbox_events` key fails closed.
- Empty `outbox_events` plus `next_cursor` null is success and performs zero receipt GETs.
- For `event_type_code=posting_receipt`, construct the AIS-pinned URNs from our stored `proposal_id` and match by equality:
  - `payload_reference = urn:cwl:accounting:posting_receipt:{proposal_id}`
  - `aggregate_reference = urn:cwl:accounting:general_journal:{proposal_id}`
- Map the matched `proposal_id` to the stored Billing `idempotency_key`.  Then `GET /posting-receipts?idempotency_key=` using that key (`invoice_draft`, `cash_receipt`, or `credit_adjustment` as published).  Do not GET `/posting-receipts` with the `payload_reference` URN.  Do not split, regex, or parse `payload_reference` to recover a key.
- Store the receipt JSON as a `posting_receipt_observation` only.  `proposal_status` stays `validated`.
- After a successful observation, or an existing observation, for the matched proposal, `POST /outbox-events/{outbox_event_id}/publish`.  AIS 403 is cross-tenant and is not retried as another tenant.  AIS 404 is unknown and does not invent a row.  GET on the publish path is AIS 405.
- Do not drain `journal_reversal` or `period_close`.
- `AIS_BASE_URL` is operator-configured https.  http is allowed only for `localhost`, `127.0.0.1`, and `::1`.  Refuse `file://`.  Never take a host from the drain request body.
- HTTP on the existing WSGI app uses the tenant pin plus the ADR 0019 key rule: `POST /v1/ais-outbox-drains`.

## Consequences

- Operators or the optional scheduler drain the AIS outbox, then store the receipt observation.  AIS may keep being polled only when the outbox is non-empty.
- The #16 posting-receipt client stays the only receipt transport.  The drain adds outbox list and publish on that client.
- A later persistent ledger can replace `MemoryUsageLedger` without changing URN equality, the stored-key lookup, or fail-closed publish rules.
