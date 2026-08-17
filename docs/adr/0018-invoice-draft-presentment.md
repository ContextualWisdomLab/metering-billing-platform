# ADR 0018: Invoice Draft Presentment HTTP

**Status:** Accepted

## Context

Operators and customers can draft, tax, credit, and collect against an `invoice_draft`, but cannot read that stored draft as a statement over HTTP.  IFRS 15 requires presentation of consideration (exclusive, tax, credits, and amount due) without treating the document as earned revenue (IFRS Foundation, 2024).  ISO 20022 keeps a commercial invoice document separate from a posted financial message (International Organization for Standardization, 2026).  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  Collection list pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  Presentment must project existing draft, tax, credit, rating, and collection rows.  It must not post, call AIS, emit a PDF, or start a web UI.

## Decision

- Expose `InvoicePresentmentService.present_invoice_draft(tenant_reference, invoice_draft_id)`.
- Project one tenant-scoped statement: identity, `drafted_at`, `currency_code`, tax exclusive/tax/inclusive (zeros when unassessed), `credited_amount`, `amount_due = max(0, inclusive - credits)`, optional collection identity and outstanding, `rating_run_id`, and lines (`metric_code`, `quantity`, `unit_amount`, `line_amount`) as exact-decimal strings.
- Expose `GET /v1/invoice-drafts/{invoice_draft_id}` on the existing WSGI app.  Tenant pin matches #15–#20.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.  Missing tenant is HTTP 422.
- Expose `GET /v1/invoice-drafts` as `{invoice_drafts, next_cursor}` summaries (`invoice_draft_id`, `amount_due`, `currency_code`, `drafted_at`) ordered by `drafted_at` then `invoice_draft_id`.
- Do not change `propose_journal`, tax, credit, or AIS contracts.  Do not add a presentment table.

## Consequences

- Operators open the draft statement, then collect or credit.
- Storybook/Figma UI can consume this JSON later.  This slice does not add a React app, PDF, or email.
- Amount due is remaining consideration after credits.  Collection outstanding remains a separate commercial fact and still reflects receipts.
