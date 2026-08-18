# ADR 0052: Unapplied Cash Application To Collection Case

**Status:** Accepted

## Context

#54 parks leftover remittance as `parked` `unapplied_cash`. Operators still cannot apply that leftover to another open collection case. Closest facts are `credit_note_application`, `payment_receipt` apply, and `unapplied_cash`. No `unapplied_cash_application` table, service, schema, or HTTP route existed.

`credit_note_application` and `payment_receipt` apply auto-settle when remaining hits zero. #46 is the explicit settle-when-zero command. This slice must reduce outstanding without settling.

`unapplied_cash` does not store remaining. This slice therefore applies the full parked amount once. Identity is one accepted row per `(tenant_account_id, unapplied_cash_id)`.

This repository is not the statutory accounting authority. Applied leftover is a commercial money fact, not a posted cash or unapplied-cash journal (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). PCI DSS keeps card PAN off the wire (PCI Security Standards Council, 2024). List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

## Decision

- Add immutable `unapplied_cash_application`. Identity is `(tenant_account_id, unapplied_cash_id)`. One parked leftover applies at most once.
- Expose `UnappliedCashApplicationService.apply_unapplied_cash(tenant_reference, unapplied_cash_id, collection_case_id, applied_amount=None, currency_code=None)`.
- Expose `POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications`. Tenant pin matches #22. Refuse PAN.
- Replay of the same tenant and leftover returns the stored `unapplied_cash_application_id` as `duplicate_replay` even if the caller names a different case.
- Apply the full parked amount. Omitting `applied_amount` uses the parked leftover. A supplied amount must equal the parked leftover. Greater than parked is `applied_amount_exceeds_parked`. Different but smaller is `applied_amount_mismatch`.
- Reduce `collection_outstanding` by the exact applied inclusive amount. Do not call `apply_collection_settlement`. Status stays `open` or `dunning` even when remaining becomes exact zero.
- Do not mutate #54 `unapplied_cash`. Status stays `parked`. The application uniqueness consumes the leftover.
- Next operator action is `collect` when remaining is positive, `settle` when remaining is exact zero and the case is still open or dunning, and `wait` when the case is already settled.
- `UnappliedCashApplicationPresentmentService` projects the stored row plus current remaining. `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` is HTTP 200 for the same tenant and 404 across tenants. `GET /v1/unapplied-cash-applications` lists `{unapplied_cash_applications, next_cursor}` ordered by `applied_at` then `unapplied_cash_application_id`.
- Fail closed on missing/cross-tenant leftover or case, leftover already applied, already settled case, currency mismatch, apply greater than parked or remaining, zero/negative leftover, negative remaining, and IEEE leftover.
- Do not invent a journal, webhook, write-off, credit note, dunning engine, PSP, AIS call, statutory numbering, or settlement command.

## Consequences

- Operators apply parked leftover onto another open case, then collect the residual or settle at exact zero through #46.
- #12, #45, #46, #49, and #54 stay unchanged.
- Journals, outbox, payment receipts, write-offs, settlements, and parked leftover rows do not grow on apply.
