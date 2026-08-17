# ADR 0024: Payment Intent HTTP Presentment

**Status:** Accepted

## Context

#11 already persists provider-neutral `payment_intent` rows from a stored collection case.  #26 presents that case so operators can collect.  Operators still cannot create or read the intent over HTTP as a statement.  POST `/v1/payment-intents` already projects from `collection_case_id`.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).  PCI DSS scope stays reduced by refusing card PAN on the wire (PCI Security Standards Council, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #11 store.  It must not capture cards, call a PSP, use a provider object ID as a primary key, call AIS, flip `proposal_status`, or add a scheduler.

## Decision

- Keep `POST /v1/payment-intents` as the #11 write.  Identity stays `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  Amount and currency come from the stored case.  Refuse PAN, CVC, and provider-secret fields on the request body.
- Expose `PaymentIntentPresentmentService.present_payment_intent(tenant_reference, payment_intent_id)`.
- Project one tenant-scoped statement: `payment_intent_id`, `tenant_reference`, `collection_case_id`, `currency_code`, `payment_amount`, `payment_intent_status` (`projected` / `cancelled` / `rejected`), and `next_operator_action` (`record_receipt` or `wait`).
- Expose `GET /v1/payment-intents/{payment_intent_id}` on the existing WSGI app.  Tenant pin matches #22.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.  Missing tenant is HTTP 422.
- Expose `GET /v1/payment-intents` as `{payment_intents, next_cursor}` summaries ordered by `projected_at` then `payment_intent_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, payment capture, AIS call, webhook event type, or scheduler.  Settlement stays #12 `payment_receipt`.

## Consequences

- Operators create a projected payment intent, then record the receipt.
- Storybook can consume this JSON.  This slice does not add a production SPA, login wall, or Figma-only work.
- Status never becomes `captured` or `settled` on this path.
