# ADR 0013: Observation-Only AIS Posting Receipt Pull

**Status:** Accepted

## Context

Journal-proposal query (#15 / ADR 0012) lets AIS pull validated `accounting_journal_proposal` documents.  AIS Draft #2 publishes `GET /posting-receipts?idempotency_key=` and an AIS-owned `accounting_posting_receipt` contract.  Operators next need that receipt as a commercial observation so Billing can reconcile without becoming the statutory poster.

RFC 9110 treats the operator POST as a non-safe trigger and the later GET of a stored observation as a safe read (Fielding et al., 2022).  ISO 20022 keeps initiation and reporting separate from posted books (International Organization for Standardization, 2026).  Helland (2012) requires that a replay of the same tenant, key, and receipt return the stored identity.

`posting_status_code` is AIS-owned.  A successful accept of a validated proposal returns `posted`.  `held`, `rejected`, and `reversed` are also AIS outcomes.  Mapping any of those onto Billing `proposal_status` would claim a posting authority this repository does not have.

## Decision

- Add `PostingReceiptPullService.pull_posting_receipt(tenant_reference, idempotency_key)`.
- Call AIS with stdlib `urllib` only: `GET {ais_base_url}/posting-receipts?idempotency_key={key}` and required `X-CWL-Tenant-Reference`.
- Copy the published AIS schema into `schemas/consumed/` as a consumer contract.  Validate the response.  Do not claim Billing owns it.
- Persist one append-only `posting_receipt_observation`.  Internal identity is `posting_receipt_observation_id`.  Natural identity is `(tenant_reference, idempotency_key)` plus `source_payload_hash` / `receipt_id`.  Never use AIS `receipt_id` as the primary key.
- Replay of the same tenant, key, and receipt returns `duplicate_replay`.  A conflicting receipt for the same key fails closed.
- HTTP 200 from AIS stores the observation.  AIS 403 is `cross_tenant` and writes zero rows.  AIS 404 is `not_yet_accepted` and writes zero rows.  Transport failure fails closed.
- Do not map `posting_status_code` onto `proposal_status`.  The source proposal stays `validated`.
- Expose `POST /v1/posting-receipt-observations` on the existing WSGI app.  Tenant pin matches #15: `X-CWL-Tenant-Reference` or body `tenant_reference`; mismatch is 422.  Response is the stored observation.
- `GET /v1/posting-receipt-observations/{idempotency_key}` reads a previously stored observation only.  It does not call AIS.  Cross-tenant or unknown keys are 404.

## Consequences

- Operators pull the receipt after AIS accept.  If AIS returns 404, they accept the proposal on AIS and retry.
- Billing remains the commercial authority.  AIS remains the posting authority.
- Credit and adjustment stay out of this slice.
