# ADR 0038: Issued Invoice From Invoice Draft

**Status:** Accepted

## Context

#8 and #21 already persist and present `invoice_draft` rows.  #19 can tax a draft.  Journal and collection flows still key on the draft.  Buyers do not yet have an immutable commercial invoice issuance artifact.  Invoice drafts stay `draft` and are explicitly not issued.  There is no existing `issued_invoice`, issue command, statutory number, or customer-facing invoice reference.

The issued document is a commercial snapshot, not a statutory invoice, tax invoice certificate, or AIS posting (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

## Decision

- Add `IssuedInvoiceService.issue_invoice(tenant_reference, invoice_draft_id, due_at=None)`.
- Persist one append-only `issued_invoice` identified by `(tenant_account_id, invoice_draft_id)`.  Replay of the same tenant and draft returns the stored `issued_invoice_id` as `duplicate_replay`.  A later `due_at` is ignored.
- Generate an opaque `issued_invoice_id`.  Do not invent sequential or statutory numbering, QR/fiscal signatures, Peppol clearance, or jurisdiction-specific compliance claims.
- Freeze currency, draft lines, and tax-exclusive/tax/inclusive totals.  Use the stored tax assessment when present; otherwise exclusive equals inclusive and tax is zero.
- Optional `due_at` is stored only when the caller supplies a valid timezone-aware instant.  Drafts have no due terms today.
- `source_payload_hash` covers draft identity, contract version, rating run, usage snapshot, currency, totals, and issued lines.  It does not include `due_at`, `issued_at`, or generated ids.
- Idempotency key is `{tenant}:issued_invoice:{issued_invoice_id}:{source_payload_hash}:v{issued_invoice_contract_version}`.
- `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` is the nested issue command.  Refuse PAN, CVC, and provider secrets.  Use #22 auth/bootstrap.
- `GET /v1/issued-invoices/{issued_invoice_id}` returns the tenant-scoped snapshot.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `issued_invoice_not_found`.  Missing tenant is HTTP 422.
- `GET /v1/issued-invoices` lists `{issued_invoices, next_cursor}` ordered by `issued_at` then `issued_invoice_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not change invoice-draft, tax-assessment, journal-proposal, collection, payment, or AIS contracts.  `invoice_draft_status` stays `draft`.  `proposal_status` stays `validated`.  Do not enqueue a webhook event, capture payment, or call AIS.

## Consequences

- Operators issue a commercial invoice, then collect or credit using existing flows.
- Draft, tax, journal, collection, payment, webhook, and AIS contracts stay unchanged.
- Storybook can consume this JSON.  This slice does not add a production SPA.
