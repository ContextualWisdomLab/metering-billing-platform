# ADR 0027: Credit Adjustment HTTP Presentment

**Status:** Accepted

## Context

#17 already records a commercial `credit_adjustment` against `invoice_draft_id` and emits the existing credit journal.  #20 already splits taxed credits.  #24 already enqueues `credit_adjustment.recorded`.  `POST /v1/credit-adjustments` already applies that write.  Operators still cannot read the stored credit as a statement.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #17 store.  It must not invent a credit shape, a journal shape, call AIS, flip `proposal_status`, or capture cards.

## Decision

- Keep `POST /v1/credit-adjustments` as the #17 write keyed on `invoice_draft_id`.  Refuse PAN, CVC, and provider-secret fields.
- Expose `CreditAdjustmentPresentmentService.present_credit_adjustment(tenant_reference, credit_adjustment_id)`.
- Project stored fields only: `credit_adjustment_id`, `tenant_reference`, `invoice_draft_id`, `currency_code`, `credit_amount`, `tax_exclusive_amount`, `tax_amount`, `credit_adjustment_status` (`recorded`), `recorded_at`, and `next_operator_action` (`wait`).
- Expose `GET /v1/credit-adjustments/{credit_adjustment_id}` as presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Expose `GET /v1/credit-adjustments` as `{credit_adjustments, next_cursor}` ordered by `recorded_at` then `credit_adjustment_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, new journal, new webhook event type, or scheduler.

## Consequences

- Operators record the credit; AIS pulls the validated journal.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Credit status never becomes `posted` on this path.
