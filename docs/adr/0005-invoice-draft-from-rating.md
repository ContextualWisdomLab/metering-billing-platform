# ADR 0005: Invoice Draft From Stored Rating Runs

**Status:** Accepted

## Context

Windowed rating already persists exact invoice-intent totals.  Buyers next need a commercial draft document they can review before issue or collection.  IFRS 15 keeps that commercial document separate from revenue recognition (IFRS Foundation, 2024).  Helland (2012) requires that a replay of the same draft command return the same stored identity.

## Decision

- Expose `InvoiceDraftService.draft_invoice` for one tenant and one stored `rating_run_id`.
- Identify a draft by `(tenant_account_id, rating_run_id)` and copy the rating run's `usage_snapshot_hash`.
- Persist append-only `invoice_draft` and `invoice_draft_line` rows whose money equals the rating-run billable totals.
- Keep `invoice_draft_status` as `draft` only.  Do not issue, collect, or post.
- Reject a missing rating run without leaking another tenant's run.
- Reject binary floating-point amounts at the draft money boundary.
- Do not emit an `accounting_journal_proposal` from this path.

## Consequences

- Known rating totals reproduce one exact draft total.
- Tenants cannot see or total each other's drafts.
- A later journal-proposal increment (`AccountingExportService`) explains every line from a persisted draft.
- Payment-provider adapters and statutory posting remain subsequent increments.
