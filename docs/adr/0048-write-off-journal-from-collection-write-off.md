# ADR 0048: Write-Off Journal Proposal From Stored Collection Write-Off

**Status:** Accepted

## Context

#49 persists an append-only `collection_write_off` that zeros leftover collection remaining. #50 publishes `write_off.recorded` on the existing #24 outbox. Invoice drafts already compose a journal proposal (#9). Payment receipts compose a cash journal (#13/#29). AIS still has nothing to pull for the write-off, so statutory books never see the AR reduction.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

#49 shipped without compose, so the command is an explicit POST rather than an automatic write-off side effect.

No existing semantic role is a write-off contra. `usage_revenue` is a credit-note unwind. `cash_receipt` is a payment. The new role is commercial `write_off_expense`, not a statutory account ID.

## Decision

- Expose `AccountingExportService.propose_write_off_journal` for one tenant and one stored `collection_write_off_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a write-off proposal by `(tenant_account_id, collection_write_off_id)`. One proposal per write-off.
- Keep the AIS-compatible idempotency key `{tenant_reference}:collection_write_off:{collection_write_off_id}:{source_payload_hash}:v{contract_version}`.
- Debit semantic `write_off_expense` and credit semantic `accounts_receivable` for the exact write-off inclusive amount. Intended book role stays `primary_statutory`.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant write-off without leaking the other tenant's document.
- Reject an optional currency that does not match the stored write-off, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed write-off is `duplicate_replay` and does not grow the store.
- Do not change collection outstanding, call AIS, invent a webhook type, tax unwind, dunning engine, PSP, payment receipt, credit note, settlement command, or statutory numbering.

## Consequences

- Operators write off leftover remaining, compose the journal, then settle.
- AIS maps semantic `write_off_expense` and `accounts_receivable`. Billing never posts.
- Invoice-draft, cash, and credit proposals remain separate identities on the same `journal_proposal` table.
- #49 write-off and #50 `write_off.recorded` stay unchanged.
