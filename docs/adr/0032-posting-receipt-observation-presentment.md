# ADR 0032: Posting Receipt Observation HTTP Presentment

**Status:** Accepted

## Context

#16 already pulls an AIS posting receipt through `PostingReceiptPullService.pull_posting_receipt` and `POST /v1/posting-receipt-observations`.  `GET /v1/posting-receipt-observations/{idempotency_key}` already returns that stored #16 write result.  #25 drain reuses the same `posting_receipt_observation` store.  Operators still cannot page stored observations with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #16/#25 store.  It must not invent a receipt shape, flip `proposal_status`, or change AIS pull/drain contracts.

## Decision

- Keep `POST /v1/posting-receipt-observations` as the #16 pull keyed on `idempotency_key`.  Refuse PAN, CVC, and provider-secret fields on the write.
- Keep `GET /v1/posting-receipt-observations/{idempotency_key}` as the existing #16 item read.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Expose `PostingReceiptObservationPresentmentService.present_posting_receipt_observation(tenant_reference, idempotency_key)`.
- Project stored fields only: `posting_receipt_observation_id`, `tenant_reference`, `source_proposal_id`, `idempotency_key`, `receipt_id`, `receipt_contract_version`, `source_payload_hash`, `posting_status_code`, `recorded_at`, `observed_at`, `posted_at` when stored, and `next_operator_action` (`wait`).
- Expose `GET /v1/posting-receipt-observations` as `{posting_receipt_observations, next_cursor}` ordered by `observed_at` then `posting_receipt_observation_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, AIS call, journal, or Billing posted status.

## Consequences

- Operators drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Observations remain commercial facts.  `proposal_status` stays `validated`.
