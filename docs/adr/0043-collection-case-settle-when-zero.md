# ADR 0043: Settle a Collection Case When Outstanding Is Exact Zero

**Status:** Accepted

## Context

#12 payment receipts settle a collection case when remaining outstanding hits zero after a positive applied amount. #17 and #45 also flip status through `apply_collection_settlement` when a credit reduces remaining to zero. Operators can still be left with an open case at exact-zero outstanding after a credit-only clearance that never went through those apply paths, or after a later mutation that leaves status `open` at remaining `0`.

A payment receipt cannot express a zero-amount credit-only settle: #12 refuses zero and negative amounts. Inventing a fake receipt would change the #12 contract. Helland (2012) requires that a replay of the same settle command return the same stored identity and never double-settle. IFRS 15 treats remaining consideration of zero as presentation, not a write-off or reversed revenue (IFRS Foundation, 2024).

## Decision

- Add `CollectionCaseSettlementService.settle_collection_case(tenant_reference, collection_case_id)`.
- Identity is `(tenant_account_id, collection_case_id)`. Replay returns the stored `collection_case_settlement_id` as `duplicate_replay` and never double-settles.
- Settle only when remaining outstanding is exact zero. Fail closed when outstanding is not zero, the case is already `settled`, or the tenant does not match.
- Reuse the existing `settled` collection-case status. Do not invent a second settlement ledger or a payment-receipt row.
- `collection_case_settlement_id` is an opaque generated identifier. Persist case, draft, optional issued invoice, currency, exact-zero remaining, `settled_at`, hash, and contract version.
- `POST /v1/collection-cases/{collection_case_id}/settlements` is the nested settle command and refuses PAN and provider secrets. `GET /v1/collection-case-settlements/{collection_case_settlement_id}` and `GET /v1/collection-case-settlements` are tenant-scoped reads.
- Do not invent a journal, tax unwind, dunning engine, PSP, AIS call, statutory numbering, write-off, or a new webhook event type. `proposal_status` stays `validated`. Payment receipts and credit-note applications stay unchanged.

## Consequences

- Operators can close an open same-tenant case that already shows exact-zero outstanding, then wait.
- Implicit #12/#17/#45 settle-when-zero on positive apply paths stays. Those settled cases reject this command as `collection_case_settled` and write zero settlement rows.
- Collection outstanding after settle remains exact zero. Status is `settled`.
