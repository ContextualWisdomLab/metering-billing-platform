# ADR 0068: Issued Credit Note Presentment Tax Assessment Link

**Status:** Accepted

## Context

#43 already persists `issued_credit_note` and presents it over HTTP. #70 exposed optional stored `tax_assessment_id` on issued-invoice item GET when frozen totals still match. Operators still could not see the stored commercial assessment that sourced a taxed credit-note split.

`issued_credit_note` does not store `tax_assessment_id`. Credit already looks up `find_tax_assessment_for_draft` and freezes a proportional exclusive/tax split of the inclusive credit. A later assessment on the same draft is not that freeze. RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). The issued document remains a commercial snapshot, not a statutory tax credit, VAT register, 세금계산서, or NTS/hometax filing (IFRS Foundation, 2024).

This slice completes the existing #43 presentment. It does not invent a second GET surface, persist a new issued-credit-note column, invent amounts, a journal, webhook, PSP, VAT period register, statutory tax invoice number, or AIS call. #34, #41, #43, and #70 stay unchanged.

## Decision

- Keep `GET /v1/issued-credit-notes/{issued_credit_note_id}` and `GET /v1/issued-credit-notes` as the existing tenant-scoped reads. Missing tenant is HTTP 422. Missing or cross-tenant note is HTTP 404 `issued_credit_note_not_found`.
- On item GET only, include optional `tax_assessment_id` when the draft already has a stored `tax_assessment` whose current split still reproduces the frozen credit exclusive and tax amounts.
- Omit `tax_assessment_id` when no assessment exists, the stored assessment cannot be split, or a later assessment would produce a different split. List summaries stay the existing closed set.
- Do not write `tax_assessment_id` onto `issued_credit_note`. Do not copy assessment amounts over frozen credit-note amounts.
- Do not add a VAT register, 세금계산서 document, NTS/hometax field, or statutory tax invoice number.

## Consequences

- Operators who already issue a credit note can open the same GET and, when the stored assessment still matches the frozen split, follow that commercial assessment.
- Credit-note issue, webhook `credit_note.issued`, and issued-invoice presentment stay the #43 and #70 contracts.
