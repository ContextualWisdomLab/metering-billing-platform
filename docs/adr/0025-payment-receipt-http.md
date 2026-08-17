# ADR 0025: Payment Receipt HTTP Presentment

**Status:** Accepted

## Context

#12 already persists applied `payment_receipt` rows and reduces collection outstanding.  #24 already enqueues `payment_receipt.applied`.  #13 cash journals stay a separate `propose_cash_journal` write with `{tenant}:cash_receipt:{payment_receipt_id}:{source_payload_hash}:v{version}`.  #27 presents the projected intent so operators can record a receipt.  Operators still cannot read the stored receipt over HTTP as a statement.  POST `/v1/payment-receipts` already applies from `payment_intent_id`.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).  PCI DSS scope stays reduced by refusing card PAN on the wire (PCI Security Standards Council, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #12 store.  It must not capture cards, call a PSP, emit a new journal shape, call AIS, flip `proposal_status`, or add a scheduler.

## Decision

- Keep `POST /v1/payment-receipts` as the #12 write.  Identity stays `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  Amount is the request `received_amount`.  Currency comes from the stored intent.  Refuse PAN, CVC, and provider-secret fields on the request body.  Do not auto-emit a cash journal; #13 stays `POST /v1/cash-journal-proposals`.
- Expose `PaymentReceiptPresentmentService.present_payment_receipt(tenant_reference, payment_receipt_id)`.
- Project one tenant-scoped statement: `payment_receipt_id`, `tenant_reference`, `payment_intent_id`, `collection_case_id`, `currency_code`, `received_amount`, `remaining_outstanding_amount`, `payment_receipt_status` (`applied`), `collection_case_status` (`open` / `dunning` / `settled`), and `next_operator_action` (`record_receipt` or `drain_or_wait`).
- Expose `GET /v1/payment-receipts/{payment_receipt_id}` on the existing WSGI app.  Tenant pin matches #22.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.  Missing tenant is HTTP 422.
- Expose `GET /v1/payment-receipts` as `{payment_receipts, next_cursor}` summaries ordered by `received_at` then `payment_receipt_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, payment capture, AIS call, new webhook event type, or scheduler.

## Consequences

- Operators record the receipt, then drain or wait for AIS to pull the cash journal.
- Storybook can consume this JSON.  This slice does not add a production SPA, login wall, or Figma-only work.
- Receipt status never becomes `captured` or `posted` on this path.
