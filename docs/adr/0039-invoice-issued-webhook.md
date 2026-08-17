# ADR 0039: Invoice Issued on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#41 persists an immutable commercial `issued_invoice` from a stored `invoice_draft` and presents it over HTTP.  #24 already registers subscriptions, enqueues accepted commercial facts, signs deliveries with HMAC-SHA256, and presents outbox and delivery metadata.  Integrations still cannot learn that a commercial invoice was issued without polling issued-invoice GET.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022).  The issued document remains a commercial snapshot, not a statutory invoice or AIS posting (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same issue command return the same stored identity and not grow the outbox.

## Decision

- Add canonical event type `invoice.issued` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `IssuedInvoiceService.issue_invoice`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`.  `source_id` is `issued_invoice_id`.
- Envelope `data` is a thin reference plus hash: `issued_invoice_id`, `invoice_draft_id`, `source_payload_hash`, `issued_invoice_contract_version`, `currency_code`, exact tax-exclusive/tax/inclusive amounts, `issued_invoice_status`, `issued_at`, optional `due_at`, `rating_run_id`, and `usage_snapshot_hash`.  Omit invoice lines, billing-account references, meter codes, PAN, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored snapshot exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed.  Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Existing subscriptions opt in by including `invoice.issued`.  HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Do not add a second webhook system, call AIS, flip `proposal_status`, invent statutory numbering, or capture payment.

## Consequences

- Operators register an https callback that includes `invoice.issued`, issue the invoice, then run deliveries.
- Issued-invoice immutability and idempotency stay the #41 contracts.
- A later persistent ledger can share one transaction for issue plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
