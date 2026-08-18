# ADR 0061: Invoice Voided on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#63 persists an append-only `issued_invoice_void` when an unused issued invoice is commercially voided and may close an unused `collection_case` as `voided`. #24 already publishes `invoice.issued`. A commercial void is not an issue, so subscribers on the existing outbox cannot see it.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022). The void remains a commercial unused-issue fact, not a journal, refund, write-off, settlement rewrite, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same void command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `invoice.voided` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `IssuedInvoiceVoidService.void_issued_invoice`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `issued_invoice_void_id`.
- Envelope `data` is a thin reference plus hash: `issued_invoice_void_id`, `issued_invoice_id`, `invoice_draft_id`, optional `collection_case_id`, `source_payload_hash`, `issued_invoice_void_contract_version`, `currency_code`, exact `voided_amount`, `issued_invoice_void_status`, and `voided_at`. Omit remaining outstanding, collection-case status, PII, payment/card data, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored void exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected void writes zero outbox rows.
- Existing subscriptions opt in by including `invoice.voided`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, a journal, a refund, a write-off, or a settlement rewrite.

## Consequences

- Operators register an https callback that includes `invoice.voided`, void an unused issued invoice, then run deliveries.
- Issued-invoice-void immutability and idempotency stay the #63 contracts.
- A later persistent ledger can share one transaction for void plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
