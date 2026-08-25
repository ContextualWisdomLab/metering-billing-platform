# ADR 0058: Unapplied-Cash Application Journal From Stored Leftover Apply

**Status:** Accepted

## Context

#54 parks leftover remittance. #60 books the park (debit `cash_receipt` / credit `unapplied_cash`). #55 applies parked leftover to another open collection case. #59 books the refund. Payment receipts already compose a cash journal for the first applied amount (#13/#29). Applying leftover to a second case never gets a semantic journal, so statutory books never see `unapplied_cash` clearing into AR.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#55 shipped without compose, so the command is an explicit POST rather than an automatic apply side effect.

The leftover/clearing role already exists as commercial `unapplied_cash` from #59/#60. `accounts_receivable` already exists. Do not invent a second leftover role or a statutory account ID.

## Decision

- Expose `AccountingExportService.propose_unapplied_cash_application_journal` for one tenant and one stored `unapplied_cash_application_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify an apply proposal by `(tenant_account_id, unapplied_cash_application_id)`. One proposal per leftover apply.
- Keep the AIS-compatible idempotency key `{tenant_reference}:unapplied_cash_application:{unapplied_cash_application_id}:{source_payload_hash}:v{contract_version}`.
- Debit semantic `unapplied_cash` and credit semantic `accounts_receivable` for the exact applied inclusive amount. Intended book role stays `primary_statutory`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant application without leaking the other tenant's document.
- Reject an optional currency that does not match the stored application, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed application is `duplicate_replay` and does not grow the store.
- Do not change leftover, apply, or refund rows, call AIS, invent a webhook type, PSP, write-off, settlement, payment-receipt rewrite, park rewrite, refund rewrite, or statutory numbering.

## Consequences

- Operators apply parked leftover, compose the apply journal, then let AIS pull.
- AIS maps semantic `unapplied_cash` and `accounts_receivable`. Billing never posts.
- Invoice-draft, cash, credit, write-off, refund, and park proposals remain separate identities on the same `journal_proposal` table.
- #13, #15, #29, #51, #54, #55, #57, #58, #59, and #60 stay unchanged.
