# ADR 0064: Commercial Collection Dispute Release

**Status:** Accepted

## Context

#66 can hold a case as `disputed`, but operators cannot release it. A held case stays stuck: no dunning, payment, credit, write-off, settle, or void. Closest facts are `collection_dispute`, `collection_case`, and the hold fail-closed rules. Inventing a second hold row would change the #66 identity. Reusing `settled` or `voided` would collapse remaining-outstanding meaning.

Helland (2012) requires that a replay of the same release command return the same stored identity and never re-flip remaining. RFC 9110 treats GET as a safe read (Fielding et al., 2022). A webhook must not grant entitlement or post accounting; `dispute.released` is a later slice.

## Decision

- Add `CollectionDisputeReleaseService.release_collection_dispute(tenant_reference, collection_dispute_id, currency_code=None)`.
- Identity is `(tenant_account_id, collection_dispute_id)`. Replay returns the stored `collection_dispute_id` as `duplicate_replay` and never changes remaining outstanding.
- Flip the existing hold row to `released`. Do not insert a second hold. Add `released_at`. Case status returns to `open`, or to `dunning` when stored notices already exist. Do not reuse `settled` or `voided`.
- After release, dunning, payment, credit apply, leftover apply, write-off, settle-when-zero, and void follow the existing open-case rules. A later hold of the same case fail-closes as `collection_dispute_released`.
- Fail closed on missing/cross-tenant dispute, not held, already released (replay only), missing case, settled or voided case, currency mismatch, or missing tenant.
- `POST /v1/collection-disputes/{collection_dispute_id}/releases` is the nested release command and refuses PAN and provider secrets. `GET /v1/collection-dispute-releases/{collection_dispute_id}` and `GET /v1/collection-dispute-releases` are tenant-scoped reads ordered by `released_at` then `collection_dispute_id`.
- Do not invent a journal, webhook, PSP, refund, write-off rewrite, void rewrite, statement rewrite, AIS call, or statutory numbering.

## Consequences

- Operators can reopen one held case without accepting money or inventing remaining.
- Replay is idempotent. One accepted release per tenant and dispute.
- Re-hold of the same case remains out of scope.
