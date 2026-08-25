# ADR 0047: Write-Off Recorded on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#49 persists an append-only `collection_write_off` when leftover collection remaining is written off. #24 already publishes `payment_receipt.applied`, `credit_note.applied`, and `collection.settled`. Write-off leaves the case `open` at exact-zero remaining, so #47 `collection.settled` never fires until #46 settles. Integrations have no commercial event for the write-off itself.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The write-off remains a commercial remaining reduction, not a journal, tax unwind, settlement, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same write-off command return the same stored identity and not grow the outbox.

Remaining outstanding after write-off is stored on `collection_write_off` as exact zero. Later case remaining can be mutated in tests or by a later settle, so the envelope uses the stored write-off remaining, not the current case remaining.

## Decision

- Add canonical event type `write_off.recorded` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `CollectionWriteOffService.write_off_collection_case`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `collection_write_off_id`.
- Envelope `data` is a thin reference plus hash: `collection_write_off_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `collection_write_off_contract_version`, `currency_code`, exact `write_off_amount`, stored exact-zero `remaining_outstanding_amount`, `collection_write_off_status`, and `written_off_at`. Omit current case status, later-mutated case remaining, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored write-off exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected write-off writes zero outbox rows.
- Existing subscriptions opt in by including `write_off.recorded`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a payment receipt, a credit note, or a settlement command.

## Consequences

- Operators register an https callback that includes `write_off.recorded`, write off leftover remaining, then run deliveries.
- Collection-write-off immutability and idempotency stay the #49 contracts.
- A later persistent ledger can share one transaction for write-off plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
