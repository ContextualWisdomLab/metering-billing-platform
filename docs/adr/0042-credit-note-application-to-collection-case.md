# ADR 0042: Credit-Note Application to a Collection Case

**Status:** Accepted

## Context

#43 persists an immutable commercial `issued_credit_note`.  #44 enqueues `credit_note.issued` on the existing commercial webhook outbox.  #10/#26 open a collection case at the full draft or tax-inclusive amount.  #17 reduces outstanding only when a case already exists at credit-record time.

Operators therefore see the full draft outstanding after a later-issued credit note.  Applying that issued credit should reduce `collection_outstanding` by the exact inclusive credit so collection works the net.

A webhook must not grant entitlement or post accounting (Fielding et al., 2022).  Helland (2012) requires that a replay of the same apply command return the same stored identity and never double-reduce money.  The issued note and original credit stay immutable commercial snapshots (IFRS Foundation, 2024).

## Decision

- Add `CreditNoteApplicationService.apply_credit_note(tenant_reference, issued_credit_note_id, collection_case_id)`.
- Identity is `(tenant_account_id, issued_credit_note_id)`.  Replay returns the stored `credit_note_application_id` as `duplicate_replay` and does not reduce outstanding again.
- Reduce `collection_outstanding` by the exact issued `tax_inclusive_amount`.  Fail closed on currency mismatch, settled case, remaining that would go negative, already-applied credit, or invoice mismatch (draft, or issued invoice when stored).
- `credit_note_application_id` is an opaque generated identifier.  Persist note, case, draft, optional issued invoice, currency, exact applied amount, `applied_at`, hashes, and versions.
- `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` is the nested apply command and refuses PAN and provider secrets.  `GET /v1/credit-note-applications/{credit_note_application_id}` and `GET /v1/credit-note-applications` are tenant-scoped reads.
- Do not invent a journal, tax unwind, dunning engine, PSP, AIS call, statutory numbering, or a new webhook event type.  `proposal_status` stays `validated`.  Payment receipts stay unchanged.

## Consequences

- Operators apply one issued credit note onto one open same-tenant case, then collect the residual.
- Issued-credit-note and credit-adjustment immutability stay the #17/#43 contracts.
- Collection outstanding after apply is the net collectible amount.
