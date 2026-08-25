# ADR 0010: Cash Journal Proposal From Stored Payment Receipts

**Status:** Accepted

## Context

Payment receipts already persist exact applied amounts.  Buyers next need an immutable `accounting_journal_proposal` that AIS can consume as cash against receivable.  IAS 7 keeps cash classification in Accounting (IFRS Foundation, n.d.).  ISO 20022 keeps initiation separate from settlement (International Organization for Standardization, 2026).  Helland (2012) requires that a replay of the same propose command return the same stored identity.

This repository is not the statutory accounting authority.  It must not post a journal, open a fiscal period, or resolve chart-account IDs.  Collection outstanding was already reduced when the receipt was applied.

## Decision

- Expose `AccountingExportService.propose_cash_journal` for one tenant and one stored `payment_receipt_id`.
- Reuse `journal_proposal` and `journal_proposal_line`.  Do not invent a second proposal table.
- Identify a cash proposal by `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)`.
- Keep the AIS-compatible idempotency key `{tenant_reference}:cash_receipt:{payment_receipt_id}:{source_payload_hash}:v{contract_version}`.
- Debit semantic `cash_receipt` and credit semantic `accounts_receivable` for the exact received amount.  Intended book role stays `primary_statutory`.
- Keep `proposal_status` inside the existing proposal lifecycle.  This path writes `validated`.  Never `posted`.
- Reject a missing or cross-tenant receipt without leaking the other tenant's document.
- Reject zero or negative amounts and binary floating-point values.
- Do not change collection outstanding, capture via a provider, or resolve statutory account IDs.

## Consequences

- Known receipt amounts reproduce one exact balanced cash/AR proposal.
- The invoice-draft AR/revenue proposal remains a separate identity and still works.
- Tenants cannot see or propose from each other's receipts.
- Operators hand the persisted cash proposal to AIS.  Catalog mapping for `cash_receipt` and `accounts_receivable` remains an AIS responsibility.
