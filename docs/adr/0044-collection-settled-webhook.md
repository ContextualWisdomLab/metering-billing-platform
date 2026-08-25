# ADR 0044: Collection Settled on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#46 persists an explicit `collection_case_settlement` when an open same-tenant case already shows exact-zero outstanding, and marks the case `settled`.  #24 already publishes `payment_receipt.applied`, `invoice.issued`, and `credit_note.issued`.  Credit-only clearance plus explicit settle does not emit a payment receipt, so subscribers cannot see the case close.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022).  The settlement remains a commercial close, not a write-off, payment receipt, or AIS posting (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same settle command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `collection.settled` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `CollectionCaseSettlementService.settle_collection_case`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`.  `source_id` is `collection_case_settlement_id`.
- Envelope `data` is a thin reference plus hash: `collection_case_settlement_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `collection_case_settlement_contract_version`, `currency_code`, exact-zero remaining outstanding, `collection_case_status`, and `settled_at`.  Omit PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, statutory identifiers, and write-off amounts.
- Call enqueue after the stored settlement exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed.  Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected settle writes zero outbox rows.  Cases already settled by #12/#45 without a `collection_case_settlement` row are not backfilled.
- Existing subscriptions opt in by including `collection.settled`.  HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a write-off, or a payment receipt.

## Consequences

- Operators register an https callback that includes `collection.settled`, settle the zero-outstanding case, then run deliveries.
- Collection-case-settlement immutability and idempotency stay the #46 contracts.
- A later persistent ledger can share one transaction for settle plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
