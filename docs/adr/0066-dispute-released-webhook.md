# ADR 0066: Dispute Released on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#67 releases a held `collection_dispute` in place and restores the `collection_case` to `open` or `dunning`. #68 already publishes `dispute.held` on the existing #24 outbox. A commercial release is not a hold, so subscribers cannot see the case reopen.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The release remains a commercial collection resume, not a journal, refund, write-off, settlement rewrite, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same release command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `dispute.released` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `CollectionDisputeReleaseService.release_collection_dispute`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `collection_dispute_id` on the same hold row. Do not invent a second release identifier.
- Envelope `data` is a thin reference plus hash: `collection_dispute_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `collection_dispute_release_contract_version`, `currency_code`, exact `remaining_outstanding_amount` at release, `collection_dispute_status`, and `released_at`. Omit collection-case status, operator action, outcome codes, `held_at`, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, statutory identifiers, and dispute-reason blobs.
- Call enqueue after the stored release exists, including on `duplicate_replay`, so a crash after flip and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row. Remaining outstanding in the envelope is the stored dispute snapshot, not a later-mutated case remaining.
- Rejected release writes zero outbox rows. Missing, cross-tenant, or not-released disputes fail closed.
- Existing subscriptions opt in by including `dispute.released`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a journal, a refund, a write-off, or a settlement rewrite.

## Consequences

- Operators register an https callback that includes `dispute.released`, release a held dispute, then run deliveries.
- Collection-dispute hold, release, and `dispute.held` immutability stay the #66, #67, and #68 contracts.
- A later persistent ledger can share one transaction for release plus enqueue; the in-memory ledger heals an orphaned flip on the next replay.
