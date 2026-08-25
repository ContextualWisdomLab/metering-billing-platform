# ADR 0063: Commercial Collection Dispute Hold

**Status:** Accepted

## Context

Operators can collect, credit, write off, settle, or void, but they cannot pause dunning on a disputed open case. Closest facts are `collection_case`, dunning notices, write-off, void, and settle-when-zero. Inventing a fake settlement or void would change those contracts. Reusing `settled` or `voided` would collapse remaining-outstanding meaning.

Helland (2012) requires that a replay of the same hold command return the same stored identity and never re-flip remaining. RFC 9110 treats GET as a safe read (Fielding et al., 2022). A webhook must not grant entitlement or post accounting; `dispute.held` is a later slice. Release is a later slice.

## Decision

- Add `CollectionDisputeService.hold_collection_case(tenant_reference, collection_case_id, currency_code=None)`.
- Identity is `(tenant_account_id, collection_case_id)`. Replay returns the stored `collection_dispute_id` as `duplicate_replay` and never changes remaining outstanding.
- Persist one append-only `collection_dispute` whose remaining snapshot equals current remaining. Dispute status is `held`. Case status becomes `disputed`. Do not reuse `settled` or `voided`.
- Only `open` or `dunning` cases may hold. Fail closed on missing/cross-tenant case, already disputed without a stored row, already settled/voided, currency mismatch, or missing tenant.
- New dunning fails closed as `collection_case_disputed`. Replay of a notice that already existed before the hold stays `#10` `duplicate_replay`.
- Payment receipt, credit apply, leftover apply, write-off, settle-when-zero, and void fail closed while held.
- `POST /v1/collection-cases/{collection_case_id}/disputes` is the nested hold command and refuses PAN and provider secrets. `GET /v1/collection-disputes/{collection_dispute_id}` and `GET /v1/collection-disputes` are tenant-scoped reads ordered by `held_at` then `collection_dispute_id`.
- Do not invent a journal, webhook, PSP, refund, write-off rewrite, void rewrite, statement rewrite, AIS call, or statutory numbering.

## Consequences

- Operators can pause dunning on one disputed open case without accepting money or closing the case.
- Replay is idempotent. One accepted hold per tenant and case while held.
- Release remains the next slice.
