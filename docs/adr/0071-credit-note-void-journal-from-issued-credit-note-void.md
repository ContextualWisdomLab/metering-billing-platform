# ADR 0071: Credit-Note Void Journal Proposal From Stored Issued-Credit-Note Void

**Status:** Accepted

## Context

#72 persists an append-only `issued_credit_note_void` that records unused issued credit. #73 publishes `credit_note.voided` on the existing #24 outbox. Credit adjustments already compose a journal proposal (#17/#52): debit `usage_revenue` (exclusive) / debit `tax_payable` when taxed / credit `accounts_receivable` (inclusive). AIS still has nothing to pull for the unused-note void, so the credit unwind never reverses.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#72 shipped without compose, so the command is an explicit POST rather than an automatic void side effect.

No new semantic role is required. The reverse of the original credit journal already uses commercial `accounts_receivable`, `usage_revenue`, and optional `tax_payable`. Do not invent a contra-revenue role or a statutory account ID.

The original credit journal is required. Unlike the #65 invoice-void journal, a missing Billing credit proposal fails closed.

## Decision

- Expose `AccountingExportService.propose_credit_note_void_journal` for one tenant and one stored `issued_credit_note_void_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a credit-note void proposal by `(tenant_account_id, issued_credit_note_void_id)`. One proposal per void.
- Keep the AIS-compatible idempotency key `{tenant_reference}:issued_credit_note_void:{issued_credit_note_void_id}:{void.source_payload_hash}:v{issued_credit_note_void_contract_version}`.
- Debit semantic `accounts_receivable` for the exact tax-inclusive voided amount and credit semantic `usage_revenue` for the issued exclusive amount. When issued tax is positive, credit `tax_payable` on the same journal. Untaxed voids stay two-line. Do not invent a new role or a second tax journal.
- Bind the original credit journal by Billing `proposal_id` plus `credit_adjustment_id` / `issued_credit_note_id` only (`reversed_journal_proposal_id` on the Python result). Fail closed if that original Billing proposal is missing. Do not emit AIS `journal_entry_id` or statutory codes such as `110100`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/issued-credit-note-voids/{issued_credit_note_void_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant void without leaking the other tenant's document.
- Reject an already-applied note, a missing original credit journal, an optional currency that does not match the stored void, and zero or negative amounts and binary floating-point values.
- Replay of an already-composed void is `duplicate_replay` and does not grow the store.
- Do not change collection, issued-credit-note, or void status, call AIS, invent a webhook type, PSP, VAT register, NTS filing, 연말정산, negative invoice, or statutory numbering.

## Consequences

- Operators void an unused issued credit note, compose the reverse journal, then let AIS pull.
- AIS maps semantic `accounts_receivable`, `usage_revenue`, and optional `tax_payable`. Billing never posts.
- Credit and credit-note-void proposals remain separate identities on the same `journal_proposal` table.
- #17, #20, #43, #45, #52, #65, #72, and #73 stay unchanged.
