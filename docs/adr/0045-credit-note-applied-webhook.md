# ADR 0045: Credit Note Applied on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#45 persists an append-only `credit_note_application` when an issued credit note is applied to an open same-tenant collection case.  #24 already publishes `payment_receipt.applied`, `credit_note.issued`, and `collection.settled`.  A partial credit leaves remaining outstanding greater than zero, so #47 `collection.settled` never fires.  Credit-only apply does not emit a payment receipt, so subscribers cannot see the apply.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022).  The application remains a commercial apply, not a journal, tax unwind, settlement, or AIS posting (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same apply command return the same stored identity and not grow the outbox.

Remaining outstanding after apply is projected from the current collection case and is not stored on `credit_note_application`.  Putting that later-mutated remaining into the envelope would change the payload hash and enqueue a second row.

## Decision

- Add canonical event type `credit_note.applied` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `CreditNoteApplicationService.apply_credit_note`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`.  `source_id` is `credit_note_application_id`.
- Envelope `data` is a thin reference plus hash: `credit_note_application_id`, `issued_credit_note_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `credit_note_application_contract_version`, `issued_credit_note_contract_version`, `issued_credit_note_source_payload_hash`, `currency_code`, exact `applied_amount`, `credit_note_application_status`, and `applied_at`.  Omit remaining outstanding (not stored on the application), PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored application exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed.  Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected apply writes zero outbox rows.
- Existing subscriptions opt in by including `credit_note.applied`.  HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a write-off, a settlement, or a payment receipt.

## Consequences

- Operators register an https callback that includes `credit_note.applied`, apply the issued credit note, then run deliveries.
- Credit-note-application immutability and idempotency stay the #45 contracts.
- A later persistent ledger can share one transaction for apply plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
