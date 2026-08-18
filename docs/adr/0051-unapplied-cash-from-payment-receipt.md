# ADR 0051: Unapplied Cash From Payment Receipt

**Status:** Accepted

## Context

#12/#28 apply a stored `payment_receipt` to one collection case and reject overpay. Operators have nowhere to park leftover remittance when a payer later supplies extra cash against that receipt, and they cannot later apply that leftover to another open case.

Closest facts are `payment_receipt`, collection remaining, `credit_note_application`, and `collection_write_off`. Write-off zeros leftover on the case. This slice parks leftover on the remittance. No `unapplied_cash` table, service, schema, or HTTP route existed.

Rewriting #12 accept to split applied versus received would change fail-closed overpay. Implied leftover `receipt_amount - applied_to_case` is exact zero for every stored #12 receipt because the full received amount was applied.

This repository is not the statutory accounting authority. Parked leftover is a commercial money fact, not a posted cash or unapplied-cash journal (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). PCI DSS keeps card PAN off the wire (PCI Security Standards Council, 2024).

## Decision

- Add immutable `unapplied_cash`. Identity is `(tenant_account_id, payment_receipt_id)`. One accepted row parks leftover against one receipt.
- Expose `UnappliedCashService.park_unapplied_cash(tenant_reference, payment_receipt_id, unapplied_amount=None, currency_code=None)`.
- Expose `POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash`. Tenant pin matches #22. Refuse PAN.
- Replay of the same tenant and receipt returns the stored `unapplied_cash_id` as `duplicate_replay`.
- #12 still rejects overpay. Do not rewrite `record_payment_receipt`.
- Omitting `unapplied_amount` fail-closes as `payment_receipt_already_consumed` because implied leftover is exact zero.
- A supplied leftover must be a positive exact decimal that does not exceed the stored receipt. Currency, when supplied, must match the receipt.
- Status is `parked`. Next operator action is `wait`. Do not auto-apply leftover to another case.
- `UnappliedCashPresentmentService` projects the stored row. `GET /v1/unapplied-cash/{unapplied_cash_id}` is HTTP 200 for the same tenant and 404 across tenants. `GET /v1/unapplied-cash` lists `{unapplied_cash, next_cursor}` ordered by `parked_at` then `unapplied_cash_id`.
- Fail closed on missing/cross-tenant receipt, leftover already parked (replay), leftover zero/negative, leftover greater than the receipt, currency mismatch, IEEE leftover, and omitted leftover.
- Do not invent a journal, webhook, write-off, settlement, credit note, dunning engine, PSP, AIS call, or statutory numbering.

## Consequences

- Operators park leftover against a stored receipt, then later apply it to another open case in a later slice.
- Receipt amount, case remaining, payment-receipt count, journals, and outbox stay unchanged on park.
- #12, #13, #28, #29, #45, #46, #49, and #53 stay unchanged.
