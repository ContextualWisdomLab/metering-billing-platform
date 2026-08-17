# ADR 0031: Tax Assessment HTTP Presentment

**Status:** Accepted

## Context

#19 already assesses a stored invoice draft through `TaxAssessmentService.assess_tax` and `POST /v1/tax-assessments`.  `GET /v1/tax-assessments/{tax_assessment_id}` already returns that stored #19 write result.  Operators still cannot page the tenant's stored assessments with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #19 store.  It must not invent a tax engine, jurisdiction, rate, journal, or status field.

## Decision

- Keep `POST /v1/tax-assessments` as the #19 assess command keyed on `invoice_draft_id` and `tax_rate_version`.  Refuse PAN, CVC, and provider-secret fields on the write.
- Keep `GET /v1/tax-assessments/{tax_assessment_id}` as the existing #19 item read.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Expose `TaxAssessmentPresentmentService.present_tax_assessment(tenant_reference, tax_assessment_id)`.
- Project stored fields only: `tax_assessment_id`, `tenant_reference`, `invoice_draft_id`, `tax_rate_version_id`, `tax_rate_version`, `tax_code`, `tax_rate`, `currency_code`, `tax_exclusive_amount`, `tax_amount`, `tax_inclusive_amount`, `source_payload_hash`, `assessed_at`, and `next_operator_action` (`propose_journal`).
- Expose `GET /v1/tax-assessments` as `{tax_assessments, next_cursor}` ordered by `assessed_at` then `tax_assessment_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, new tax engine, journal, or status field.

## Consequences

- Operators publish a tax rate, assess the draft, then propose the journal and let AIS pull.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Tax assessments remain commercial facts, not books.
