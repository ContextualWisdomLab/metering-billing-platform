# ADR 0060: Issued Invoice Void

**Status:** Accepted

## Context

Operators can issue a commercial invoice (#41) and see it on a billing-account statement (#62), but they cannot void a bad issue. Closest facts are `issued_invoice`, `collection_case`, payment receipt, credit-note application, write-off, leftover apply, and settle-when-zero. No commercial void existed. Reusing `settled` would collapse a bad issue into a paid-or-closed collection fact.

Helland (2012) requires that a replay of the same void command return the same stored identity and never re-close the case. IFRS 15 treats a commercial void of unused issued consideration as presentation, not reversed revenue or a statutory posting (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). A webhook must not grant entitlement or post accounting; journal and webhook slices can follow later.

## Decision

- Add `IssuedInvoiceVoidService.void_issued_invoice(tenant_reference, issued_invoice_id, currency_code=None)`.
- Identity is `(tenant_account_id, issued_invoice_id)`. Replay returns the stored `issued_invoice_void_id` as `duplicate_replay` and never re-closes the case.
- Persist one append-only `issued_invoice_void` whose `voided_amount` is the issued tax-inclusive amount and whose stored remaining is exact zero. Status is `recorded`. The issued snapshot stays `issued`.
- Fail closed unless the related `collection_case` has had no payment receipt, credit-note apply, unapplied-cash apply, or write-off. Remaining must still equal the issued inclusive amount. A projected payment intent does not block.
- If the case is still `open` or `dunning`, close it as `voided` at exact-zero remaining. Do not reuse `settled`. Settle-when-zero then fail-closes as `collection_case_voided`.
- `issued_invoice_void_id` is an opaque generated identifier. Persist invoice, draft, optional case, currency, voided amount, exact-zero remaining, `voided_at`, hash, and contract version.
- `POST /v1/issued-invoices/{issued_invoice_id}/voids` is the nested void command and refuses PAN and provider secrets. `GET /v1/issued-invoice-voids/{issued_invoice_void_id}` and `GET /v1/issued-invoice-voids` are tenant-scoped reads ordered by `voided_at` then `issued_invoice_void_id`.
- Do not invent a journal, webhook, PSP, refund, write-off rewrite, statement rewrite, AIS call, or statutory numbering. #41, #45, #46, #49, #54, #55, #57, and #62 stay immutable.

## Consequences

- Operators can void one unused same-tenant issued invoice. Replay is idempotent. One accepted row per tenant and issued invoice.
- An unused open or dunning case closes as `voided` and is omitted from aging. Journal and webhook can follow later.
