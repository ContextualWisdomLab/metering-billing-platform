# ADR 0053: Unapplied Cash Applied on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#55 persists an append-only `unapplied_cash_application` when parked leftover is applied to another open same-tenant collection case. #24 already publishes `payment_receipt.applied` and `credit_note.applied`. Apply leftover does not emit a payment receipt or credit note, and remaining may stay positive, so subscribers cannot see the apply.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The application remains a commercial apply, not a journal, write-off, settlement, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same apply command return the same stored identity and not grow the outbox.

Remaining outstanding after apply is projected from the current collection case and is not stored on `unapplied_cash_application`. Putting that later-mutated remaining into the envelope would change the payload hash and enqueue a second row.

## Decision

- Add canonical event type `unapplied_cash.applied` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `UnappliedCashApplicationService.apply_unapplied_cash`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `unapplied_cash_application_id`.
- Envelope `data` is a thin reference plus hash: `unapplied_cash_application_id`, `unapplied_cash_id`, `payment_receipt_id`, `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id` when stored, `source_payload_hash`, `unapplied_cash_application_contract_version`, `currency_code`, exact `applied_amount`, `unapplied_cash_application_status`, and `applied_at`. Omit remaining outstanding (not stored on the application), PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored application exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected apply writes zero outbox rows.
- Existing subscriptions opt in by including `unapplied_cash.applied`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a write-off, a settlement command, or a payment-receipt rewrite.

## Consequences

- Operators register an https callback that includes `unapplied_cash.applied`, apply parked leftover, then run deliveries.
- Unapplied-cash-application immutability and idempotency stay the #55 contracts.
- A later persistent ledger can share one transaction for apply plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
