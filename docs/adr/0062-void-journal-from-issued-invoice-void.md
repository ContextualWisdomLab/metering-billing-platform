# ADR 0062: Void Journal Proposal From Stored Issued-Invoice Void

**Status:** Accepted

## Context

#63 persists an append-only `issued_invoice_void` that zeros unused issued consideration. #64 publishes `invoice.voided` on the existing #24 outbox. Invoice drafts already compose a journal proposal (#9): debit `accounts_receivable` / credit `usage_revenue` / credit `tax_payable` when taxed. Credit adjustments already reverse that (#17/#52). AIS still has nothing to pull for the void, so statutory AR never reverses.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#63 shipped without compose, so the command is an explicit POST rather than an automatic void side effect.

No new semantic role is required. The reverse of the original invoice journal already uses commercial `usage_revenue`, optional `tax_payable`, and `accounts_receivable`. Do not invent a contra-revenue role or a statutory account ID.

## Decision

- Expose `AccountingExportService.propose_void_journal` for one tenant and one stored `issued_invoice_void_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a void proposal by `(tenant_account_id, issued_invoice_void_id)`. One proposal per void.
- Keep the AIS-compatible idempotency key `{tenant_reference}:issued_invoice_void:{issued_invoice_void_id}:{void.source_payload_hash}:v{issued_invoice_void_contract_version}`.
- Debit semantic `usage_revenue` for the issued exclusive amount and credit semantic `accounts_receivable` for the exact inclusive voided amount. When issued tax is positive, debit `tax_payable` on the same journal. Do not invent a new role or a second tax journal.
- Bind the original invoice journal by Billing `proposal_id` / stored invoice-draft journal identity only. Do not emit AIS `journal_entry_id` or statutory codes such as `110100`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/issued-invoice-voids/{issued_invoice_void_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant void without leaking the other tenant's document.
- Reject an optional currency that does not match the stored void, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed void is `duplicate_replay` and does not grow the store.
- Do not change collection or issued-invoice status, call AIS, invent a webhook type, PSP, write-off, settlement rewrite, negative invoice, or statutory numbering.

## Consequences

- Operators void an unused issued invoice, compose the reverse journal, then let AIS pull.
- AIS maps semantic `usage_revenue`, optional `tax_payable`, and `accounts_receivable`. Billing never posts.
- Invoice-draft and void proposals remain separate identities on the same `journal_proposal` table.
- #9, #17, #41, #52, #63, and #64 stay unchanged.
