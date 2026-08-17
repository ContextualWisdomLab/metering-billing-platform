# ADR 0007: Collection Case And Dunning From Stored Invoice Drafts

**Status:** Accepted

## Context

Invoice drafts already persist exact commercial totals.  Buyers next need a tenant-scoped collection case they can dunn before a payment intent exists.  IEEE (2019) and Cowlishaw (2009) forbid binary floating-point outstanding.  Helland (2012) requires that a replay of the same open command return the same stored identity.  IFRS 15 keeps collection reminders separate from revenue recognition (IFRS Foundation, 2024).

This path must not capture payment, issue a statutory invoice, call a payment provider, or post a journal.

## Decision

- Expose `CollectionCaseService.open_collection_case` for one tenant and one stored `invoice_draft_id`.
- Identify a case by `(tenant_account_id, invoice_draft_id)` and copy the draft's exact outstanding.
- Persist append-only `collection_case` and `collection_dunning_event` rows.
- Keep commercial status in `open` or `dunning`.  Do not persist `paid`, `written_off`, or `posted`.
- Record `first_notice` and `overdue_notice` as commercial reminders with `occurred_at` from the ledger clock.  The same case and notice code replay the stored event.
- Reject a missing or cross-tenant draft or case without leaking the other tenant's document.
- Reject zero or negative outstanding and binary floating-point amounts.
- Do not open a payment intent, call a payment provider, or post to AIS from this path.

## Consequences

- Known invoice-draft totals reproduce one exact outstanding.
- Tenants cannot see or collect each other's cases.
- Operators open the case, then send a dunning notice.  Payment capture remains the next increment.
