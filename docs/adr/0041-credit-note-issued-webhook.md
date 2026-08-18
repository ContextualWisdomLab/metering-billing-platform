# ADR 0041: Credit Note Issued on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#43 persists an immutable commercial `issued_credit_note` from a stored `credit_adjustment` and presents it over HTTP.  #42 already enqueues `invoice.issued` on the existing #24 commercial webhook outbox.  #43 explicitly deferred `credit_note.issued`.  Integrations can subscribe to invoice issuance but cannot learn that a commercial credit note was issued without polling issued-credit-note GET.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022).  The issued document remains a commercial snapshot, not a statutory credit note or AIS posting (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same issue command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `credit_note.issued` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `IssuedCreditNoteService.issue_credit_note`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`.  `source_id` is `issued_credit_note_id`.
- Envelope `data` is a thin reference plus hash: `issued_credit_note_id`, `credit_adjustment_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `issued_credit_note_contract_version`, `currency_code`, exact tax-exclusive/tax/inclusive amounts, `issued_credit_note_status`, `issued_at`, and the closed `credit_reason_code`.  Omit lines, PII, reason prose, PAN, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored snapshot exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed.  Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Existing subscriptions opt in by including `credit_note.issued`.  HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, or capture payment.  Rejected issue writes zero outbox rows.

## Consequences

- Operators register an https callback that includes `credit_note.issued`, issue the credit note, then run deliveries.
- Issued-credit-note immutability and idempotency stay the #43 contracts.
- A later persistent ledger can share one transaction for issue plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
