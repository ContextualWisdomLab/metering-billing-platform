# ADR 0046: Commercial Collection Write-Off of Leftover Remaining

**Status:** Accepted

## Context

Operators can apply credits (#45) and payments (#12) and settle a collection case only when remaining outstanding is exact zero (#46). Leftover uncollectable remaining — a partial credit, a remainder after payment, or a case that will never be paid — cannot be closed. There is no commercial write-off. Closest facts are `credit_note_application`, `payment_receipt`, and `collection_case_settlement`. Inventing a fake receipt or credit would change those contracts. Auto-settling on write-off would collapse #46.

Helland (2012) requires that a replay of the same write-off command return the same stored identity and never re-zero outstanding. IFRS 15 treats a commercial write-off of remaining consideration as presentation, not reversed revenue or a statutory posting (IFRS Foundation, 2024). A webhook must not grant entitlement or post accounting (Fielding et al., 2022); `write_off.recorded` is a later slice.

## Decision

- Add `CollectionWriteOffService.write_off_collection_case(tenant_reference, collection_case_id, write_off_amount=None, currency_code=None)`.
- Identity is `(tenant_account_id, collection_case_id)`. Replay returns the stored `collection_write_off_id` as `duplicate_replay` and never re-zeros outstanding.
- Persist one append-only `collection_write_off` whose `write_off_amount` is the current remaining inclusive amount and whose stored remaining is exact zero. Status is `recorded`. Case status stays `open` or `dunning`.
- Body may omit amount, or require amount to equal current remaining. Fail closed on amount mismatch, currency mismatch, missing case, already settled, remaining already zero, or negative remaining.
- Do not auto-settle. #46 remains the explicit settle-when-zero command. After write-off, remaining is `"0"` and #46 can settle.
- `collection_write_off_id` is an opaque generated identifier. Persist case, draft, optional issued invoice, currency, write-off amount, exact-zero remaining, `written_off_at`, hash, and contract version.
- `POST /v1/collection-cases/{collection_case_id}/write-offs` is the nested write-off command and refuses PAN and provider secrets. `GET /v1/collection-write-offs/{collection_write_off_id}` and `GET /v1/collection-write-offs` are tenant-scoped reads ordered by `written_off_at` then `collection_write_off_id`.
- Do not invent a journal, tax unwind, dunning engine, PSP, AIS call, statutory numbering, payment receipt, credit note, settlement command, or webhook event. `proposal_status` stays `validated`. Issued credit notes, applications, settlements, payment receipts, and outbox events stay immutable.

## Consequences

- Operators can write off leftover same-tenant remaining, then settle at exact zero with #46.
- Replay is idempotent. One accepted row per tenant and case.
- `write_off.recorded` remains the next slice.
