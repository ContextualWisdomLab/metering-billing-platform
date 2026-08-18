# ADR 0070: Credit Note Voided on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#72 persists an append-only `issued_credit_note_void` when an unused issued credit note is commercially voided. #24 already publishes `credit_note.issued` and `credit_note.applied`. A commercial unused-note void is not an issue or an apply, so subscribers on the existing outbox cannot see it.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The void remains a commercial unused-credit fact, not a journal, refund, write-off, settlement rewrite, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same void command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `credit_note.voided` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `IssuedCreditNoteVoidService.void_issued_credit_note`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `issued_credit_note_void_id`.
- Envelope `data` is a thin reference plus hash: `issued_credit_note_void_id`, `issued_credit_note_id`, `credit_adjustment_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `issued_credit_note_void_contract_version`, `currency_code`, exact tax-inclusive `voided_amount`, `issued_credit_note_void_status`, and `voided_at`. Omit lines, remaining outstanding, collection identity or status, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, AIS ids, and statutory identifiers.
- Call enqueue after the stored void exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected void writes zero outbox rows.
- Existing subscriptions opt in by including `credit_note.voided`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a journal, a refund, a write-off, or a settlement rewrite. #24, #43, #45, #63, #64, #71, and #72 stay immutable in contract. Do not reopen `invoice.voided`.

## Consequences

- Operators register an https callback that includes `credit_note.voided`, void an unused issued credit note, then run deliveries.
- Issued-credit-note-void immutability and idempotency stay the #72 contracts.
- A later persistent ledger can share one transaction for void plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
- A later slice can compose a void journal so AIS can pull the reverse of the issued-credit-note journal.
