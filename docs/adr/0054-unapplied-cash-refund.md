# ADR 0054: Unapplied Cash Refund

**Status:** Accepted

## Context

#54 parks leftover remittance as `parked` `unapplied_cash`. #55 can apply that leftover to another open collection case. Operators still cannot return unused parked leftover to the payer.

Closest facts are `unapplied_cash`, `unapplied_cash_application`, `payment_receipt`, and `collection_write_off`. Write-off zeros leftover remaining on a case. Payment-receipt apply consumes remittance against a case. No `unapplied_cash_refund` table, service, schema, or HTTP route existed.

This repository is not the statutory accounting authority. A leftover refund is a commercial money fact, not a posted cash journal, PSP capture, or statutory credit (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). PCI DSS keeps card PAN off the wire (PCI Security Standards Council, 2024). List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

## Decision

- Add immutable `unapplied_cash_refund`. Identity is `(tenant_account_id, unapplied_cash_id)`. One parked leftover refunds at most once.
- Expose `UnappliedCashRefundService.refund_unapplied_cash(tenant_reference, unapplied_cash_id, refund_amount=None, currency_code=None)`.
- Expose `POST /v1/unapplied-cash/{unapplied_cash_id}/refunds`. Tenant pin matches #22. Refuse PAN.
- Replay of the same tenant and leftover returns the stored `unapplied_cash_refund_id` as `duplicate_replay`.
- Refund the full parked amount. Omitting `refund_amount` uses the parked leftover. A supplied amount must equal the parked leftover.
- Do not mutate #54 `unapplied_cash`. Status stays `parked`. Refund uniqueness consumes the leftover.
- Fail closed when leftover is already applied (#55), leftover is not parked, leftover is missing or cross-tenant, currency mismatches, or the amount is zero, negative, mismatched, or IEEE.
- Apply fail-closes when a refund already exists so leftover cannot be both refunded and applied.
- `UnappliedCashRefundPresentmentService` projects the stored row plus current leftover status. `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` is HTTP 200 for the same tenant and 404 across tenants. `GET /v1/unapplied-cash-refunds` lists `{unapplied_cash_refunds, next_cursor}` ordered by `refunded_at` then `unapplied_cash_refund_id`.
- Do not invent a journal, webhook, write-off, settlement, credit note, dunning engine, PSP capture, AIS call, or statutory numbering. `refund.recorded` is a later slice.

## Consequences

- Operators refund unused parked leftover as a commercial fact, then later publish a webhook in a later slice.
- #12, #45, #46, #54, #55, and #56 stay unchanged except the additive apply fail-closed when leftover is already refunded.
- Journals, outbox, payment receipts, write-offs, settlements, applications, and parked leftover rows do not grow on refund.
