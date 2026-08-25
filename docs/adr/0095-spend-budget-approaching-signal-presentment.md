# ADR 0095: Spend-Budget Approaching-Signal HTTP Presentment

**Status:** Accepted

## Context

#104 observes a published commercial `spend_budget` and enqueues one existing #24 `spend_budget.approaching` outbox event on first `utilization_status=at` the documented `budget_amount` (ADR 0082; ADR 0093). #105 presents that write through Storybook fixtures. Operators still cannot GET the live approaching-signal or the stored first-at outbox observation for one published spend budget.

This repository is not the statutory accounting authority. IFRS 15 treats a commercial approaching observation as control evidence, not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024).

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. The #104 approaching-signal write, #105 approaching Storybook, GET over-signal, GET evaluation, GET budget-status, persist, SpendBudget/RatedSpend/BudgetStatus/SpendBudgetOver Storybooks, and `spend_budget.published` stay unchanged.

## Decision

- Expose `SpendBudgetApproachingSignalPresentmentService.present_spend_budget_approaching_signal(tenant_reference, spend_budget_id)`.
- Reuse `SpendBudgetEvaluationPresentmentService` so remaining/over math matches `GET /v1/spend-budgets/{id}/evaluation`. Project the live observation into the existing approaching-signal envelope. Outcome on GET is `accepted`.
- Include `webhook_outbox_events` as zero or one existing webhook-outbox presentment for `event_type_code` `spend_budget.approaching` and `source_id` `spend_budget_id`. Do not invent a second event type or a parallel outbox envelope.
- Expose `GET /v1/spend-budgets/{spend_budget_id}/approaching-signal` on the existing WSGI app. Tenant pin matches #22. Same tenant is HTTP 200. Unknown or cross-tenant is HTTP 404 with no leak. Missing `X-CWL-Tenant-Reference` (and no query `tenant_reference`) is HTTP 422. Pin the header to the commercial `tenant_account` and fail closed as HTTP 403 when the stored billing account belongs to another tenant.
- Replay is a safe GET and writes no outbox row. An at budget that was never POSTed presents live `utilization_status=at` with zero approaching-signal rows. After first-at enqueue, GET presents the current evaluation plus the stored first-at outbox row. Next operator action stays `wait` on the approaching-signal envelope. Notify threshold stays ADR 0082 `utilization_status=at`.
- Do not persist an evaluation snapshot. Do not mutate `spend_budget`. Do not hard-stop rating, ingest, or invoice draft. Do not enqueue a webhook or compose a journal.

## Consequences

- Operators inspect one published budget's live approaching-signal and stored first-at outbox row without polling Storybook fixtures.
- POST approaching-signal stays the #104 write. Over write/GET, evaluation GET, budget-status GET, persist, Storybook, and `spend_budget.published` stay unchanged.
- #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.

## References

Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP semantics* (RFC 9110). RFC Editor. https://doi.org/10.17487/RFC9110

IEEE. (2019). *IEEE standard for floating-point arithmetic* (IEEE Std 754-2019). IEEE. https://doi.org/10.1109/IEEESTD.2019.8766229

IFRS Foundation. (2024). *IFRS 15 revenue from contracts with customers*. IFRS Foundation.

PCI Security Standards Council. (2024). *Payment Card Industry Data Security Standard: Requirements and testing procedures* (Version 4.0.1). PCI Security Standards Council.
