# ADR 0049: Credit Journal Proposal From Stored Credit Adjustment

**Status:** Accepted

## Context

#17 records an append-only `credit_adjustment` and already composes one validated credit journal. #20 unwinds `tax_payable` on that same journal when the draft is taxed. #9 composes AR/revenue from an invoice draft. #51 composes write-off/AR from a stored write-off through an explicit POST. AIS can pull credit journals after accept, but there was no explicit compose/replay command on `AccountingExportService` or `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals`.

This repository is not the statutory accounting authority. It must not post a journal, open a fiscal period, resolve chart-account IDs, or call AIS (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same compose command return the same stored identity. A webhook must not grant entitlement or post accounting (Fielding et al., 2022).

The credit journal already exists. This slice exposes it. It does not invent a second proposal table, a contra-revenue role, or a second tax unwind.

## Decision

- Expose `AccountingExportService.propose_credit_journal` for one tenant and one stored `credit_adjustment_id`.
- Reuse `journal_proposal` and `journal_proposal_line`. Do not invent a second proposal table.
- Identify a credit proposal by `(tenant_account_id, credit_adjustment_id)`. One proposal per credit.
- Keep the existing Billing idempotency key `{tenant_reference}:credit_adjustment:{credit_adjustment_id}:{credit.source_payload_hash}:v{credit_adjustment_contract_version}`.
- Debit semantic `usage_revenue` for the stored exclusive amount and credit semantic `accounts_receivable` for the exact credit inclusive amount. When stored tax is positive, reuse the existing #20 `tax_payable` debit on the same journal. Do not invent a contra-revenue role or a second tax journal.
- Keep `proposal_status` inside the existing proposal lifecycle. This path writes `validated`. Never `posted`.
- Expose `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals`. Credit accept remains the first compose. The POST is an explicit compose or `duplicate_replay`. AIS pull stays existing `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- Reject a missing or cross-tenant credit without leaking the other tenant's document.
- Reject an optional currency that does not match the stored credit, and reject zero or negative amounts and binary floating-point values.
- Replay of an already-composed credit is `duplicate_replay` and does not grow the store.
- Do not change collection outstanding, call AIS, invent a webhook type, write-off, settlement, payment, statutory numbering, or a second journal store.

## Consequences

- Operators record the credit, then may POST the explicit compose; AIS pulls the same validated proposal.
- AIS maps semantic `usage_revenue`, optional `tax_payable`, and `accounts_receivable`. Billing never posts.
- Invoice-draft, cash, credit, and write-off proposals remain separate identities on the same `journal_proposal` table.
- #9, #13, #15, #17, #20, #29, #43, #45, and #51 stay unchanged.
