# ADR 0065: Dispute Held on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#66 persists an append-only `collection_dispute` when an open or dunning `collection_case` is commercially held. #67 releases that hold in place. #24 already publishes `collection.settled` and `invoice.voided`. A commercial hold is not a settlement or a void, so subscribers on the existing outbox cannot see it.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The hold remains a commercial collection pause, not a journal, refund, write-off, settlement rewrite, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same hold command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `dispute.held` to the existing #24 known event vocabulary and subscription/outbox schemas. Do not add `dispute.released` in this slice.
- On first successful `CollectionDisputeService.hold_collection_case`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `collection_dispute_id`.
- Envelope `data` is a thin reference plus hash: `collection_dispute_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `collection_dispute_contract_version`, `currency_code`, exact `remaining_outstanding_amount` at hold, `collection_dispute_status`, and `held_at`. Omit collection-case status, operator action, outcome codes, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, statutory identifiers, and dispute-reason blobs.
- Call enqueue after the stored hold exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row. Remaining outstanding in the envelope is the stored hold snapshot, not a later-mutated case remaining.
- Rejected hold writes zero outbox rows. Missing or cross-tenant dispute fail closed as rejected hold.
- Existing subscriptions opt in by including `dispute.held`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a journal, a refund, a write-off, or a settlement rewrite.

## Consequences

- Operators register an https callback that includes `dispute.held`, hold a collection case, then run deliveries.
- Collection-dispute hold and release immutability and idempotency stay the #66 and #67 contracts.
- A later persistent ledger can share one transaction for hold plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
