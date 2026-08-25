# ADR 0072: Account-Statement Void Totals For Unused Issues And Unused Credits

**Status:** Accepted

## Context

#62 presents one billing-account statement grouped by currency. #63 persists unused issued-invoice voids. #72 persists unused issued-credit-note voids. The statement still risked treating a voided issue as live issued consideration and an unused voided credit as if it were applied commercial credit.

This repository is not the statutory accounting authority. A statement is presentation of stored commercial facts, not a posted trial balance (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21). Issued snapshots stay `issued` after void; rewriting `issued_invoice_total` would erase history.

#62, #63, #65, #72, and #74 stay unchanged as command contracts. This slice extends the existing presentment only.

## Decision

- Keep existing per-currency exact inclusive `issued_invoice_total`, `open_collection_remaining`, `applied_credit_total`, `write_off_total`, `parked_unapplied_cash`, and `refunded_unapplied_cash`.
- Add exact inclusive `voided_invoice_total` and `voided_credit_total` on each currency row.
- Keep `issued_invoice_total` as the issued snapshot total for exclusive invoice-draft lines. Do not subtract voids from that bucket.
- Sum `voided_invoice_total` from stored unused `issued_invoice_void` rows whose invoice-draft lines belong exclusively to the requested billing account.
- Keep `applied_credit_total` as applied credits only. Sum `voided_credit_total` from stored unused `issued_credit_note_void` rows whose credit/draft lines belong exclusively to that account. Do not count a voided unused note as applied credit.
- Keep the exclusive-line attribution rule. Mixed-account and lineless drafts stay omitted.
- Keep `GET /v1/billing-accounts/{billing_account_id}/statement` as a safe read. Write no money fact.
- Fail closed: missing tenant HTTP 422, missing account HTTP 404, cross-tenant HTTP 403.
- Do not invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, or collection-status flip.

## Consequences

- Operators can see unused voids beside issued and applied history without rewriting those buckets.
- AIS consume and compose commands stay unchanged.
- #62, #63, #65, #72, and #74 stay immutable in command contracts.
