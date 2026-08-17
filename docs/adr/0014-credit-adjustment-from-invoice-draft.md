# ADR 0014: Commercial Credit Adjustment From Invoice Draft

**Status:** Accepted

## Context

Posting-receipt observation (#16 / ADR 0013) lets operators store an AIS receipt without flipping `proposal_status`.  A buyer still cannot reverse part of an invoice intent before collections continue.  IFRS 15 treats a later price concession or billing correction as variable consideration, not as a posted statutory reversal (IFRS Foundation, 2024).  ISO 20022 keeps a commercial credit note separate from a posted `camt` settlement message (International Organization for Standardization, 2026).  Helland (2012) requires that a replay of the same credit command return the stored identity.

This repository is not the statutory accounting authority.  It must not post a journal, open a fiscal period, emit statutory account IDs, or call AIS.  AIS already maps semantic `usage_revenue` and `accounts_receivable`.  Tax, refund-to-card, and chargeback stay out of this slice.

## Decision

- Expose `CreditAdjustmentService.record_credit_adjustment(tenant_reference, invoice_draft_id, credit_amount, credit_reason_code)`.
- Persist one append-only `credit_adjustment`.  Internal identity is `credit_adjustment_id`.  Natural identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)`.
- Accept an exact `Decimal` `credit_amount` greater than zero that does not exceed `remaining_adjustable = drafted_total_amount − prior accepted credits`.  Currency is copied from the draft.
- Keep `credit_reason_code` in the closed set `rating_correction`, `goodwill`, `billing_error`.
- If a `collection_case` exists, reduce outstanding by the same amount.  Remaining zero marks the case `settled`.  Do not go negative.  If outstanding is already less than the credit, reject `credit_exceeds_outstanding` and write zero rows.
- Emit one balanced `accounting_journal_proposal` that reuses `journal_proposal`: debit `usage_revenue` and credit `accounts_receivable` for the exact credit amount.  `intended_book_role_code` stays `primary_statutory`.  `proposal_status` stays `validated` and is never `posted`.
- Keep the AIS-compatible idempotency key `{tenant}:credit_adjustment:{credit_adjustment_id}:{source_payload_hash}:v{contract_version}`.
- Replay of the same tenant, draft, amount, reason, source-payload hash, and contract version returns the same `credit_adjustment_id` and `proposal_id` as `duplicate_replay`.
- Expose `POST /v1/credit-adjustments` on the existing WSGI app.  Tenant pin matches #15/#16.  The response is the credit-adjustment contract plus `proposal_id`.  Credit proposals appear on existing `GET /v1/journal-proposals`.
- Optional `GET /v1/credit-adjustments/{credit_adjustment_id}` is tenant-scoped.  Cross-tenant or unknown identifiers are 404 and do not call AIS.

## Consequences

- Operators record the credit, then let AIS pull the validated proposal.
- Full credits settle an open collection case.  Partial credits leave residual outstanding.
- A credit after payment settlement fails closed when outstanding is already below the credit.
- Tenants cannot see or credit each other's drafts.
- AIS contract is unchanged: `proposal_status` stays proposal-only, and Billing still does not post.
