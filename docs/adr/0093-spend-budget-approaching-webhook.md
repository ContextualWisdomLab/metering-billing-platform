# ADR 0093: Spend Budget Approaching on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#101 observes a published commercial `spend_budget` and enqueues one existing #24 `spend_budget.over` outbox event on first `utilization_status=over`. #103 presents the live over-signal plus that stored first-over row. Operators and buyers still cannot see that a published budget first reached the documented `budget_amount` while utilization remained `at`, not `over`.

No stored notify percentage exists on the #82 publish contract. Identity is `(tenant_account_id, billing_account_id, window_started_at, window_ended_at, currency_code, source_payload_hash, spend_budget_contract_version)`, and the hash covers exact `budget_amount` (ADR 0079). Adding a new threshold field would widen publish, persist, hash, and presentment. ADR 0082 already documents `utilization_status=at` as exact equality to that stored `budget_amount` while remaining and over stay complementary zeros. That already-documented crossing is the notify threshold for this slice.

A webhook must not grant entitlement, persist evaluation, or post accounting (Fielding et al., 2022). The approaching observation remains a commercial control signal, not a reservation, hard-stop, journal, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that replay of the same approaching observation not grow the outbox. IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024).

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope. GET evaluation and GET over-signal must stay safe reads (Fielding et al., 2022).

## Decision

- Expose a write command, not a GET side-effect. `SpendBudgetApproachingSignalService.observe_spend_budget_approaching` reuses `SpendBudgetEvaluationPresentmentService` so remaining/over math matches `GET /v1/spend-budgets/{id}/evaluation`.
- `POST /v1/spend-budgets/{spend_budget_id}/approaching-signal` is HTTP 200 for same-tenant published budgets. Unknown or cross-tenant is HTTP 404 with no leak. Missing `X-CWL-Tenant-Reference` (and no body `tenant_reference`) is HTTP 422. A stored budget whose billing account belongs to another commercial `tenant_account` is HTTP 403.
- Add canonical event type `spend_budget.approaching` to the existing #24 known event vocabulary and subscription/outbox schemas.
- Enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact` only when `utilization_status` is `at`. `source_id` is `spend_budget_id`. First-at-wins looks up that source on the stored budget's `tenant_account_id`.
- Envelope `data` is a thin reference plus hash: `spend_budget_id`, `billing_account_id`, `spend_budget_contract_version`, `source_payload_hash`, `currency_code`, exact `budget_amount`, exact `remaining_amount`, `window_started_at`, `window_ended_at`, `spend_budget_status`, and `utilization_status=at`. Omit over (so currencies stay unmixed), rated lines, PII, PAN, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- under and over write zero approaching-signal outbox rows. Replay of the same `spend_budget_id` source is `duplicate_replay` and does not enqueue a second row. The replay result is the current evaluation; the outbox row keeps the first-at facts. A crash after compute and before enqueue is healed by the next observe. Rejected observe writes zero rows.
- Existing subscriptions opt in by including `spend_budget.approaching`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Pin `X-CWL-Tenant-Reference` to the commercial `tenant_account` and fail closed on missing or mismatch. Do not auto-create tenants.
- GET evaluation, GET budget-status, GET over-signal, persist, Storybook, `spend_budget.published`, and `spend_budget.over` stay unchanged. Do not persist an evaluation snapshot, mutate the budget, hard-stop rating, ingest, or invoice draft, emit a journal, call AIS, invent a dimension-scoped budget, emit `retained_earnings` or 310100, or add VAT/NTS adapters. Do not invent a default notify percentage.

## Consequences

- Operators register an https callback that includes `spend_budget.approaching`, observe a published budget after it is first at the documented `budget_amount`, then run deliveries.
- Evaluation stays the #93 safe GET. Over-signal write and GET stay the #101/#103 contracts. Publish and persist stay the #82/#100 contracts. #85 may later reserve, commit, or deny execution from these same stored facts. This slice does not.

## References

Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP semantics* (RFC 9110). RFC Editor. https://doi.org/10.17487/RFC9110

Helland, P. (2012). Idempotence is not a medical condition. *Communications of the ACM, 55*(5), 56–65. https://doi.org/10.1145/2160718.2160734

IEEE. (2019). *IEEE standard for floating-point arithmetic* (IEEE Std 754-2019). IEEE. https://doi.org/10.1109/IEEESTD.2019.8766229

IFRS Foundation. (2024). *IFRS 15 revenue from contracts with customers*. IFRS Foundation.

PCI Security Standards Council. (2024). *Payment Card Industry Data Security Standard: Requirements and testing procedures* (Version 4.0.1). PCI Security Standards Council.
