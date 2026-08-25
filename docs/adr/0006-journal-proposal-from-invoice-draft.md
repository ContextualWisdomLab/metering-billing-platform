# ADR 0006: Journal Proposal From Stored Invoice Drafts

**Status:** Accepted

## Context

Invoice drafts already persist exact commercial totals.  Buyers next need an immutable `accounting_journal_proposal` that AIS can consume.  IFRS 15 keeps that export separate from revenue recognition (IFRS Foundation, 2024).  IFRS 18 keeps statement presentation in AIS (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same propose command return the same stored identity.

This repository is not the statutory accounting authority.  It must not post a journal, open a fiscal period, or resolve chart-account IDs.

## Decision

- Expose `AccountingExportService.propose_journal` for one tenant and one stored `invoice_draft_id`.
- Identify a proposal by `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)`.
- Persist append-only `journal_proposal` and `journal_proposal_line` rows whose debit total equals the credit total.
- Use semantic account roles (`accounts_receivable`, `usage_revenue`) and an intended book role.  Do not invent statutory account IDs or AIS APIs.
- Keep `proposal_status` inside the existing proposal lifecycle (`draft`, `validated`, `exported`, `rejected`).  This path writes `validated`.  Never `posted`.
- Reject a missing or cross-tenant draft without leaking the other tenant's document.
- Reject a zero draft total because the published line contract requires debit XOR credit.
- Reject binary floating-point amounts at the proposal money boundary.
- Do not issue, collect, or call a payment provider from this path.

## Consequences

- Known invoice-draft totals reproduce one exact balanced proposal.
- Tenants cannot see or propose from each other's drafts.
- Operators hand the proposal to AIS.  Posting, fiscal-period control, and statutory mapping remain AIS responsibilities.
- Issued invoices, payment-provider adapters, and payment capture remain subsequent increments.  Commercial collection cases are a later increment that still must not post journals.
