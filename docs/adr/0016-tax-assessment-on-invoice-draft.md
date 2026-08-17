# ADR 0016: Tax Assessment on Invoice Draft

**Status:** Accepted

## Context

A buyer can draft invoice intent and propose a two-line AR/revenue journal (#9 / ADR 0006), but cannot attach commercial tax.  IFRS 15 treats consideration as the amount expected in exchange for the promised goods or services; amounts collected on behalf of tax authorities are not revenue (IFRS Foundation, 2024).  IAS 12 governs how tax is presented in the statutory books, not how Billing invents a rate (IFRS Foundation, n.d.).  ISO 4217 defines currency minor units used to round a tax product (International Organization for Standardization, 2015).  OECD VAT/GST guidelines keep a published rate separate from later invoice calculation (OECD, 2017).  Helland (2012) requires that a replay of the same tax command return the stored identity.

This repository is not the statutory accounting authority.  It must not call an OSS tax engine, store exemption certificates, post journals, or emit statutory account IDs.  AIS maps the new semantic role `tax_payable`.  Tax-payable unwind on credit is a later slice.

## Decision

- Expose `TaxRateService.publish_tax_rate(tenant_reference, tax_code, tax_rate)`.
- Persist one tenant-scoped `tax_rate_schedule` identified by `(tenant_account_id, tax_code)` and one append-only `tax_rate_version`.  Closed `tax_code` values are `vat`, `gst`, and `sales_tax`.  `tax_rate` is an exact `Decimal` in `[0, 1]`.
- Replay of the same tenant, tax code, rate, and contract version returns the stored `tax_rate_version` as `duplicate_replay`.  A later distinct rate increments `version_number`.  A published version is never edited.
- Expose `TaxAssessmentService.assess_tax(tenant_reference, invoice_draft_id, tax_rate_version)`.
- Persist one append-only `tax_assessment` per draft.  `tax_exclusive_amount` is `drafted_total_amount`.  `tax_amount` is `round_half_even(exclusive * tax_rate)` to the ISO 4217 minor units in the closed exponent table (`0` for JPY and KRW, `2` for the listed two-decimal currencies).  Unknown currencies fail closed.  `tax_inclusive_amount` is exclusive plus tax.
- Replay of the same tenant, draft, tax-rate version, and source-payload hash returns the same `tax_assessment_id`.
- Reject `assess_tax` after a collection case is open (`tax_after_collection_opened`).  `open_collection_case` uses `tax_inclusive_amount` when an assessment exists, otherwise `drafted_total_amount`.
- `propose_journal` on a taxed draft emits one balanced validated proposal: debit `accounts_receivable` for the inclusive amount, credit `usage_revenue` for the exclusive amount, credit `tax_payable` for the tax amount.  A half-even product that rounds to zero omits the `tax_payable` line so every persisted line stays debit XOR credit; the source-payload hash still includes the tax facts so a zero-tax assessment cannot collide with an untaxed draft.  Untaxed drafts keep the existing two-line AR/revenue proposal.  The idempotency key stays `{tenant}:invoice_draft:{invoice_draft_id}:{source_payload_hash}:v{version}`; the hash includes tax lines when assessed.
- Credit `remaining_adjustable` uses the inclusive amount when an assessment exists.  The credit journal stays two-line revenue/AR.  Tax-payable unwind is next.
- Expose `POST /v1/tax-rates`, `GET /v1/tax-rates`, `GET /v1/tax-rate-versions/{tax_rate_version}`, `POST /v1/tax-assessments`, and `GET /v1/tax-assessments/{tax_assessment_id}` on the existing WSGI app.  Tenant pin matches #15–#18.  Cross-tenant reads are 404.

## Consequences

- Operators publish a tax rate, assess the draft, then propose the journal and let AIS pull.
- AIS must map `tax_payable` onto its chart.  Billing does not emit statutory account IDs.
- Collection outstanding matches the inclusive commercial amount when tax was assessed first.
- Credits reduce inclusive remaining without reversing `tax_payable` in this slice.
- No address, nexus, exemption, or Stripe Tax engine is introduced.
