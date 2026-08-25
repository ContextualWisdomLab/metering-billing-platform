# ADR 0089: Durable PostgreSQL Published Spend Budget

**Status:** Accepted

## Context

#82 publishes one append-only commercial `spend_budget`. #93 evaluates that
published row against already-rated spend. #97 lists those evaluations for one
billing account. #95 enqueues `spend_budget.published` on the existing #24
outbox. Storybook presents those contracts. Those writes and reads still used
`MemoryUsageLedger`. A successful in-process publish therefore did not prove
that a restart preserved the buyer-visible commercial budget.

Issue #84 remains the broader durable-runtime backlog. This slice persists only
the already-published budget row. Issue #85 atomic authorization, quotas,
entitlements, reserve/commit/release, and hard-stop stay later.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped `find_spend_budget`,
  `get_spend_budget`, `insert_spend_budget`, and `list_spend_budgets`.
- Persist one published row per successful publish: `tenant_account_id`,
  `billing_account_id`, `spend_budget_id`, `currency_code`, exact
  `budget_amount`, half-open window instants, `published_at`,
  `spend_budget_status`, `source_payload_hash`, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same publish identity is `duplicate_replay` and does not
  insert a second row. A crash after insert and before outbox enqueue is
  healed by the next replay. Rejected publish writes zero rows.
- Commit the published row and the existing `spend_budget.published` outbox
  event in one repository transaction.
- Keep `GET /v1/spend-budgets/{id}`, evaluation, budget-status, Storybook, and
  the #24 outbox contracts unchanged. Reads that already work in-memory keep
  working when the row is loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, utilization, `rated_amount`,
  remaining/over, journals, AIS receipts, VAT/NTS, `retained_earnings` /
  310100, tenant auto-create, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

A published commercial spend budget now survives process restart as commercial
truth. Evaluation, budget-status, and Storybook continue to project that
stored row. Issue #84 remains open for the other authoritative records and
production recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
