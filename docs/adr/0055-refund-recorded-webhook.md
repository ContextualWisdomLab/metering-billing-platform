# ADR 0055: Refund Recorded on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#57 persists an append-only `unapplied_cash_refund` when unused parked leftover is returned to the payer as a commercial fact. #24 already publishes `unapplied_cash.applied`, `payment_receipt.applied`, and `write_off.recorded`. A leftover refund is not an apply, a receipt apply, or a write-off, so subscribers cannot see the refund.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The refund remains a commercial leftover-return fact, not a PSP capture, cash-movement adapter, journal, write-off, settlement, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same refund command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `refund.recorded` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `UnappliedCashRefundService.refund_unapplied_cash`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `unapplied_cash_refund_id`.
- Envelope `data` is a thin reference plus hash: `unapplied_cash_refund_id`, `unapplied_cash_id`, `payment_receipt_id`, `source_payload_hash`, `unapplied_cash_refund_contract_version`, `currency_code`, exact `refund_amount`, `unapplied_cash_refund_status`, and `refunded_at`. Omit payment-intent and collection-case ids, parked leftover snapshot, leftover status, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored refund exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected refund writes zero outbox rows.
- Existing subscriptions opt in by including `refund.recorded`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a write-off, a settlement command, a journal, or a payment-receipt rewrite.

## Consequences

- Operators register an https callback that includes `refund.recorded`, refund unused parked leftover, then run deliveries.
- Unapplied-cash-refund immutability and idempotency stay the #57 contracts.
- A later persistent ledger can share one transaction for refund plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
