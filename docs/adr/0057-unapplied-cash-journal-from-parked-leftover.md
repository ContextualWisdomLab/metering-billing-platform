# ADR 0057: Unapplied-Cash Journal Proposal From Parked Leftover

**Status:** Accepted

## Context

#54 parks leftover remittance as `unapplied_cash`. #59 books a leftover refund (debit `unapplied_cash` / credit `cash_receipt`). Payment receipts already compose a cash journal for the applied amount (#13/#29). The leftover itself never gets a semantic journal, so statutory books never see cash sitting as liability/clearing until refund.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#54 shipped without compose, so the command is an explicit POST rather than an automatic park side effect.

The leftover/clearing role already exists as commercial `unapplied_cash` from #59. Do not invent a second leftover role or a statutory account ID.

## Decision

- Expose `AccountingExportService.propose_unapplied_cash_journal` for one tenant and one stored `unapplied_cash_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a leftover proposal by `(tenant_account_id, unapplied_cash_id)`. One proposal per parked leftover.
- Keep the AIS-compatible idempotency key `{tenant_reference}:unapplied_cash:{unapplied_cash_id}:{source_payload_hash}:v{contract_version}`.
- Debit semantic `cash_receipt` and credit semantic `unapplied_cash` for the exact parked leftover inclusive amount. Intended book role stays `primary_statutory`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant leftover without leaking the other tenant's document.
- Reject leftover whose status is not `parked`.
- Reject an optional currency that does not match the stored leftover, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed leftover is `duplicate_replay` and does not grow the store.
- Do not change leftover or refund rows, call AIS, invent a webhook type, PSP, write-off, settlement, payment-receipt rewrite, refund rewrite, or statutory numbering.

## Consequences

- Operators park leftover remittance, compose the leftover journal, then let AIS pull.
- AIS maps semantic `cash_receipt` and `unapplied_cash`. Billing never posts.
- Invoice-draft, cash, credit, write-off, and refund proposals remain separate identities on the same `journal_proposal` table.
- #13, #15, #29, #51, #54, #57, #58, and #59 stay unchanged.
