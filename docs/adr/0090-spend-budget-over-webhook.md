# ADR 0090: Spend Budget Over on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#82 persists and presents one append-only commercial `spend_budget`. #93 compares that published budget to already-rated spend. #95 enqueues `spend_budget.published` on the existing #24 commercial webhook outbox. #100 persists the published row in PostgreSQL. Operators and buyers still cannot see that a published budget was first observed as `utilization_status=over` unless they poll evaluation.

A webhook must not grant entitlement, persist evaluation, or post accounting (Fielding et al., 2022). The over observation remains a commercial control signal, not a reservation, hard-stop, journal, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that replay of the same over observation not grow the outbox. IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024).

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. GET evaluation must stay a safe read (Fielding et al., 2022).

## Decision

- Add a write command, not a GET side-effect. `SpendBudgetOverSignalService.observe_spend_budget_over` reuses `SpendBudgetEvaluationPresentmentService` so remaining/over math matches `GET /v1/spend-budgets/{id}/evaluation`.
- `POST /v1/spend-budgets/{spend_budget_id}/over-signal` is HTTP 200 for same-tenant published budgets. Unknown or cross-tenant is HTTP 404 with no leak. Missing `X-CWL-Tenant-Reference` (and no body `tenant_reference`) is HTTP 422. A stored budget whose billing account belongs to another commercial `tenant_account` is HTTP 403.
- Add canonical event type `spend_budget.over` to the existing #24 known event vocabulary and subscription/outbox schemas.
- Enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact` only when `utilization_status` is `over`. `source_id` is `spend_budget_id`. First-over-wins looks up that source on the stored budget's `tenant_account_id`.
- Envelope `data` is a thin reference plus hash: `spend_budget_id`, `billing_account_id`, `spend_budget_contract_version`, `source_payload_hash`, `currency_code`, exact `budget_amount`, exact `over_amount`, `window_started_at`, `window_ended_at`, `spend_budget_status`, and `utilization_status=over`. Omit remaining (so currencies stay unmixed), rated lines, PII, PAN, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- under and at write zero over-signal outbox rows. Replay of the same `spend_budget_id` source is `duplicate_replay` and does not enqueue a second row. A crash after compute and before enqueue is healed by the next observe. Rejected observe writes zero rows.
- Existing subscriptions opt in by including `spend_budget.over`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Pin `X-CWL-Tenant-Reference` to the commercial `tenant_account` and fail closed on missing or mismatch. Do not auto-create tenants.
- GET evaluation, GET budget-status, persist, Storybook, and `spend_budget.published` stay unchanged. Do not persist an evaluation snapshot, mutate the budget, hard-stop rating, ingest, or invoice draft, emit a journal, call AIS, invent a dimension-scoped budget, emit `retained_earnings` or 310100, or add VAT/NTS adapters.

## Consequences

- Operators register an https callback that includes `spend_budget.over`, observe a published budget after it is first over, then run deliveries.
- Evaluation stays the #93 safe GET. Publish and persist stay the #82/#100 contracts. #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.
