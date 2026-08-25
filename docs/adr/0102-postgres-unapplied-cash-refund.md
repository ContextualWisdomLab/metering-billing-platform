# ADR 0102: Durable PostgreSQL Unapplied Cash Refund

**Status:** Accepted

## Context

#57 refunds one unused parked leftover as an append-only commercial
`unapplied_cash_refund`. GET presentment already projects that row plus
current leftover status. `unapplied_cash` and leftover-apply already
reload from `PostgresUsageLedger`. The leftover itself therefore survives
restart, but leftover refund still had only the `MemoryUsageLedger` path.
The #112 leftover-refund lookup stub SELECTed then returned `None`. A
successful in-process leftover refund therefore did not prove that a
restart preserved the buyer-visible refund or that leftover-apply could
fail-close on a real refund row.

#112 also left leftover-apply insert and outstanding reduction in
separate transactions. A crash after insert and before reduce left the
apply row persisted with remaining un-reduced; replay returned
`duplicate_replay` without reducing. That mirrors the #110 unused-void
window #112 healed.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified leftover refund, then heals leftover-apply
replay remaining. Journals and #85 atomic authorization stay unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_unapplied_cash_refund`, `get_unapplied_cash_refund`,
  `insert_unapplied_cash_refund`, and
  `list_unapplied_cash_refunds_for_tenant`. Replace the #112 leftover-refund
  lookup stub with a real reload.
- Persist one recorded row per successful leftover refund:
  `tenant_account_id`, `unapplied_cash_id`, `payment_receipt_id`,
  `payment_intent_id`, `collection_case_id`, `unapplied_cash_refund_id`,
  exact `refund_amount`, exact parked `unapplied_amount` snapshot,
  `refunded_at`, `unapplied_cash_refund_status=recorded`, source hash,
  and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and parked leftover is `duplicate_replay` and
  does not insert a second row or mutate remaining. A concurrent insert
  identity race classifies as `duplicate_replay` when insert returns the
  stored id. A crash after insert and before the existing
  `refund.recorded` outbox enqueue is healed by the next replay.
  Rejected refund writes zero rows.
- Refund the full parked amount. The parked leftover row stays `parked`.
  A leftover-refund row fail-closes leftover-apply after restart.
- On leftover-apply `duplicate_replay`, if remaining still equals unused
  opened outstanding (not yet reduced), apply the reduction. Do not
  double-reduce. Do not settle. Already-reduced remaining stays as-is.
- Keep `GET /v1/unapplied-cash-refunds/{id}` and list presentment
  unchanged. Reads that already work in-memory keep working when the row is
  loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, statutory numbers, VAT/NTS,
  `retained_earnings` / 310100, tenant auto-create, journals, AIS
  receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

A leftover refund now survives process restart as commercial truth. GET
presentment continues to project that stored row plus current leftover
status. Leftover-apply fail-closes on a real refund row after restart.
Leftover-apply replay heals an insert-then-reduce crash without
double-reducing or settling. Issue #84 remains open for the other
authoritative records and production recovery/deployment controls. Issue #85
remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
