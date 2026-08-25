# ADR 0084: Spend Budget Published on the Commercial Webhook Outbox

**Status:** Accepted

## Context

#82 persists and presents one append-only commercial `spend_budget`. #93 compares that published budget to already-rated spend. #24 already publishes accepted commercial facts through `webhook_outbox_event`. Operators and buyers still cannot see that a budget was published unless they poll.

A webhook must not grant entitlement, persist evaluation, or post accounting (Fielding et al., 2022). The published budget remains a commercial control fact, not a reservation, rated-spend comparison, journal, or AIS posting (IFRS Foundation, 2024). Helland (2012) requires that a replay of the same publish identity return the same stored `spend_budget_id` and not grow the outbox. IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024).

Issue #85 will later enforce atomic authorization, quotas, and entitlements; that control engine is out of scope.

## Decision

- Add canonical event type `spend_budget.published` to the existing #24 known event vocabulary and subscription/outbox schemas.
- On first successful `SpendBudgetService.publish_spend_budget`, enqueue one `webhook_outbox_event` through existing `enqueue_accepted_fact`. `source_id` is `spend_budget_id`.
- Envelope `data` is a thin reference plus hash: `spend_budget_id`, `billing_account_id`, `spend_budget_contract_version`, `source_payload_hash`, `currency_code`, exact `budget_amount`, `window_started_at`, `window_ended_at`, `published_at`, and `spend_budget_status`. Omit rated spend, remaining/over, utilization, lines, PII, PAN, tenant secrets, API keys, webhook secrets/hashes, raw documents, and statutory identifiers.
- Call enqueue after the stored budget exists, including on `duplicate_replay`, so a crash after insert and before enqueue is healed. Replay of the same tenant, event type, source, and payload hash returns the stored outbox row.
- Rejected publish writes zero outbox rows.
- Existing subscriptions opt in by including `spend_budget.published`. HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, callback SSRF policy, delivery attempts, run summary, delivery presentment, and outbox presentment stay unchanged.
- Pin `X-CWL-Tenant-Reference` to the commercial `tenant_account` and fail closed on missing or mismatch. Do not auto-create tenants.
- Do not persist evaluation, mutate the budget after publish, hard-stop rating, ingest, or invoice draft, emit a journal, call AIS, invent a dimension-scoped budget, emit `retained_earnings` or 310100, or add VAT/NTS adapters.

## Consequences

- Operators register an https callback that includes `spend_budget.published`, publish a commercial budget, then run deliveries.
- Spend-budget immutability and identity stay the #82 contracts. Evaluation stays the #93 safe GET.
- A later persistent ledger can share one transaction for publish plus enqueue; the in-memory ledger heals an orphaned insert on the next replay.
