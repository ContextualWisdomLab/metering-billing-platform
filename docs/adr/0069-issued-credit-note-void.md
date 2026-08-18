# ADR 0069: Issued Credit Note Void

**Status:** Accepted

## Context

Operators can issue a commercial credit note (#43) and apply it (#45), but they cannot void a bad unused credit note the way they void an unused issued invoice (#63). Closest facts are `issued_credit_note`, `credit_note_application`, and `issued_invoice_void`. No commercial credit-note void existed.

Helland (2012) requires that a replay of the same void command return the same stored identity and never rewrite history. IFRS 15 treats a commercial void of unused issued credit as presentation, not reversed revenue or a statutory posting (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). A webhook must not grant entitlement or post accounting; journal and webhook slices can follow later.

## Decision

- Add `IssuedCreditNoteVoidService.void_issued_credit_note(tenant_reference, issued_credit_note_id, currency_code=None)`.
- Identity is `(tenant_account_id, issued_credit_note_id)`. Replay returns the stored `issued_credit_note_void_id` as `duplicate_replay`.
- Persist one append-only `issued_credit_note_void` whose `voided_amount` is the issued tax-inclusive credit. Status is `recorded`. The issued snapshot stays `issued`.
- Fail closed if the credit note has already been applied to a `collection_case`. Remaining on any case is unchanged because the note cannot have been applied.
- After a void exists, `CreditNoteApplicationService.apply_credit_note` fail-closes as `issued_credit_note_voided`. Existing-application replay still wins so an already-applied note stays replayable.
- `issued_credit_note_void_id` is an opaque generated identifier. Persist note, credit, draft, optional issued invoice, currency, voided amount, `voided_at`, hash, and contract version.
- `POST /v1/issued-credit-notes/{issued_credit_note_id}/voids` is the nested void command and refuses PAN and provider secrets. `GET /v1/issued-credit-note-voids/{issued_credit_note_void_id}` and `GET /v1/issued-credit-note-voids` are tenant-scoped reads ordered by `voided_at` then `issued_credit_note_void_id`.
- Do not invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory account, negative invoice, or AIS call. #43, #45, #63, and #71 stay immutable.

## Consequences

- Operators can void one unused same-tenant issued credit note. Replay is idempotent. One accepted row per tenant and issued credit note.
- Collection remaining is unchanged. Journal and webhook can follow later.
