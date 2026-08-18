# ADR 0056: Refund Journal Proposal From Stored Unapplied-Cash Refund

**Status:** Accepted

## Context

#57 persists an append-only `unapplied_cash_refund` when unused parked leftover is returned to the payer. #58 publishes `refund.recorded` on the existing #24 outbox. Payment receipts already compose a cash journal (#13/#29). Write-offs compose a journal (#51). AIS still has nothing to pull for the leftover refund, so statutory books never see cash going back.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#57 shipped without compose, so the command is an explicit POST rather than an automatic refund side effect.

No existing semantic role is parked leftover/clearing. `cash_receipt` is a payment. `accounts_receivable` is billed consideration. The new role is commercial `unapplied_cash`, not a statutory account ID.

## Decision

- Expose `AccountingExportService.propose_refund_journal` for one tenant and one stored `unapplied_cash_refund_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a refund proposal by `(tenant_account_id, unapplied_cash_refund_id)`. One proposal per leftover refund.
- Keep the AIS-compatible idempotency key `{tenant_reference}:unapplied_cash_refund:{unapplied_cash_refund_id}:{source_payload_hash}:v{contract_version}`.
- Debit semantic `unapplied_cash` and credit semantic `cash_receipt` for the exact refund inclusive amount. Intended book role stays `primary_statutory`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant refund without leaking the other tenant's document.
- Reject an optional currency that does not match the stored refund, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed refund is `duplicate_replay` and does not grow the store.
- Do not change leftover or refund rows, call AIS, invent a webhook type, PSP, write-off, settlement, payment-receipt rewrite, or statutory numbering.

## Consequences

- Operators refund unused parked leftover, compose the journal, then let AIS pull.
- AIS maps semantic `unapplied_cash` and `cash_receipt`. Billing never posts.
- Invoice-draft, cash, credit, and write-off proposals remain separate identities on the same `journal_proposal` table.
- #13, #15, #29, #51, #57, and #58 stay unchanged.
