# ADR 0067: Issued Invoice Presentment Tax Assessment Link

**Status:** Accepted

## Context

#41 already persists `issued_invoice` and presents it over HTTP. #21 presents invoice drafts. #62 rolls issued totals onto a billing-account statement. #69 is an unrelated commercial webhook. Operators still could not see the stored commercial `tax_assessment_id` that sourced a taxed issue.

`issued_invoice` does not store `tax_assessment_id`. Issue already looks up `find_tax_assessment_for_draft` and freezes exclusive/tax/inclusive amounts. A later assessment on the same draft is not that freeze. RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). The issued document remains a commercial snapshot, not a statutory tax invoice, VAT register, 세금계산서, or NTS/hometax filing (IFRS Foundation, 2024).

This slice completes the existing #41 presentment. It does not invent a second GET surface, persist a new issued-invoice column, invent amounts, a journal, webhook, PSP, void rewrite, statement rewrite, VAT period register, statutory tax invoice number, or AIS call. #21, #34, #41, #62, and #69 stay unchanged.

## Decision

- Keep `GET /v1/issued-invoices/{issued_invoice_id}` and `GET /v1/issued-invoices` as the existing tenant-scoped reads. Missing tenant is HTTP 422. Missing or cross-tenant invoice is HTTP 404 `issued_invoice_not_found`.
- On item GET only, include optional `tax_assessment_id` when the draft already has a stored `tax_assessment` whose exclusive, tax, and inclusive amounts still equal the frozen issued totals.
- Omit `tax_assessment_id` when no assessment exists or the stored assessment amounts no longer match the issued snapshot. List summaries stay the existing closed set.
- Do not write `tax_assessment_id` onto `issued_invoice`. Do not copy assessment amounts over frozen issued amounts.
- Do not add a VAT register, 세금계산서 document, NTS/hometax field, or statutory tax invoice number.

## Consequences

- Operators who already issue an invoice can open the same GET and, when taxed, follow the stored commercial assessment.
- Issue, webhook `invoice.issued`, account statement, draft presentment, and dispute-release outbox stay the #41, #21, #62, and #69 contracts.
