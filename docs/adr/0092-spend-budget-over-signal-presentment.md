# ADR 0092: Spend-Budget Over-Signal HTTP Presentment

**Status:** Accepted

## Context

#101 observes a published commercial `spend_budget` and enqueues one existing #24 `spend_budget.over` outbox event on first `utilization_status=over`. #102 presents that write through Storybook fixtures. Operators still cannot GET the live over-signal or the stored first-over outbox observation for one published spend budget.

This repository is not the statutory accounting authority. IFRS 15 treats a commercial over observation as control evidence, not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024).

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. The #101 over-signal write, GET evaluation, GET budget-status, persist, Storybook, and `spend_budget.published` stay unchanged.

## Decision

- Expose `SpendBudgetOverSignalPresentmentService.present_spend_budget_over_signal(tenant_reference, spend_budget_id)`.
- Reuse `SpendBudgetEvaluationPresentmentService` so remaining/over math matches `GET /v1/spend-budgets/{id}/evaluation`. Project the live observation into the existing over-signal envelope. Outcome on GET is `accepted`.
- Include `webhook_outbox_events` as zero or one existing webhook-outbox presentment for `event_type_code` `spend_budget.over` and `source_id` `spend_budget_id`. Do not invent a second event type or a parallel outbox envelope.
- Expose `GET /v1/spend-budgets/{spend_budget_id}/over-signal` on the existing WSGI app. Tenant pin matches #22. Same tenant is HTTP 200. Unknown or cross-tenant is HTTP 404 with no leak. Missing `X-CWL-Tenant-Reference` (and no query `tenant_reference`) is HTTP 422. Pin the header to the commercial `tenant_account` and fail closed as HTTP 403 when the stored billing account belongs to another tenant.
- Replay is a safe GET and writes no outbox row. An over budget that was never POSTed presents live `utilization_status=over` with zero over-signal rows. After first-over enqueue, GET presents the current evaluation plus the stored first-over outbox row. Next operator action stays `wait` on the over-signal envelope.
- Do not persist an evaluation snapshot. Do not mutate `spend_budget`. Do not hard-stop rating, ingest, or invoice draft. Do not enqueue a webhook or compose a journal.

## Consequences

- Operators inspect one published budget's live over-signal and stored first-over outbox row without polling Storybook fixtures.
- POST over-signal stays the #101 write. Evaluation GET, budget-status GET, persist, Storybook, and `spend_budget.published` stay unchanged.
- #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.
