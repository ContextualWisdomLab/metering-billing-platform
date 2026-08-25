# ADR 0040: Issued Credit Note From Credit Adjustment

**Status:** Accepted

## Context

#17, #20, and #30 persist `credit_adjustment` rows with exact tax-exclusive, tax, and inclusive amounts plus a validated journal proposal.  Those rows are accounting evidence.  Buyers do not yet have an immutable customer-facing commercial credit-note artifact.  There is no existing `issued_credit_note`, issue command, statutory number, or credit-note line table.  `credit_adjustment` is a single-amount fact.

The issued document is a commercial snapshot, not a statutory credit note, tax credit certificate, or AIS posting (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

## Decision

- Add `IssuedCreditNoteService.issue_credit_note(tenant_reference, credit_adjustment_id)`.
- Persist one append-only `issued_credit_note` identified by `(tenant_account_id, credit_adjustment_id)`.  Replay of the same tenant and credit returns the stored `issued_credit_note_id` as `duplicate_replay`.
- Generate an opaque `issued_credit_note_id`.  Do not invent sequential or statutory numbering, QR/fiscal signatures, Peppol clearance, or jurisdiction-specific compliance claims.
- Freeze currency and tax-exclusive/tax/inclusive credit amounts from the stored adjustment.  Inclusive equals stored `credit_amount`.  Exclusive plus tax must equal inclusive.
- Preserve `credit_adjustment_id`, `invoice_draft_id`, source hashes, contract versions, `issued_at`, and the closed `credit_reason_code`.  Store `issued_invoice_id` only when `find_issued_invoice(tenant, invoice_draft_id)` already exists.  Omit the field when absent.
- Do not invent credit-note lines.  The adjustment has no line table.
- `source_payload_hash` covers credit identity, draft identity, optional issued invoice id, versions, currency, amounts, and the credit source hash.  It does not include `issued_at` or the generated note id.
- Idempotency key is `{tenant}:issued_credit_note:{issued_credit_note_id}:{source_payload_hash}:v{issued_credit_note_contract_version}`.
- `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` is the nested issue command.  Refuse PAN, CVC, and provider secrets.  Use #22 auth/bootstrap.
- `GET /v1/issued-credit-notes/{issued_credit_note_id}` returns the tenant-scoped snapshot.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `issued_credit_note_not_found`.  Missing tenant is HTTP 422.
- `GET /v1/issued-credit-notes` lists `{issued_credit_notes, next_cursor}` ordered by `issued_at` then `issued_credit_note_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not change credit-adjustment, tax-unwind, journal-proposal, invoice, collection, payment, or AIS contracts.  `credit_adjustment` stays `recorded`.  `proposal_status` stays `validated`.  Do not enqueue a webhook event, capture payment, or call AIS.  `credit_note.issued` can be a later slice.

## Consequences

- Operators issue a commercial credit note.  The validated journal remains available for AIS.
- Credit, tax unwind, journal, invoice, collection, payment, webhook, and AIS contracts stay unchanged.
- Storybook can consume this JSON.  This slice does not add a production SPA.
