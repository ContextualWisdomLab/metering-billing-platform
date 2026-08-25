# ADR 0101: Durable PostgreSQL Unapplied Cash Application

**Status:** Accepted

## Context

#55 applies one parked leftover as an append-only commercial
`unapplied_cash_application` onto one open same-tenant collection case.
GET presentment already projects that row plus current remaining outstanding.
`unapplied_cash` already reloads from `PostgresUsageLedger`. The leftover
itself therefore survives restart, but leftover-apply still had only the
`MemoryUsageLedger` path. The #110 unused-void leftover-apply lookup stub
SELECTed then returned `()`. A successful in-process leftover-apply
therefore did not prove that a restart preserved the buyer-visible apply or
that unused-invoice void could fail-close on a real leftover-apply row.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified leftover-apply. Leftover refund, unused invoice
voids as a new write, journals, and #85 atomic authorization stay unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_unapplied_cash_application`, `get_unapplied_cash_application`,
  `insert_unapplied_cash_application`,
  `list_unapplied_cash_applications_for_tenant`, and
  `apply_unapplied_cash_to_collection_case`. Replace the #110 leftover-apply
  list stub with a real reload.
- Persist one applied row per successful leftover-apply: `tenant_account_id`,
  `unapplied_cash_id`, `collection_case_id`, `payment_receipt_id`,
  `invoice_draft_id`, `unapplied_cash_application_id`, exact
  `applied_amount`, `applied_at`, `unapplied_cash_application_status=applied`,
  source hash, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and parked leftover is `duplicate_replay` and
  does not insert a second row or reduce remaining again. A concurrent insert
  identity race classifies as `duplicate_replay` when insert returns the
  stored id. A crash after insert and before the existing
  `unapplied_cash.applied` outbox enqueue is healed by the next replay.
  Rejected apply writes zero rows.
- Apply the full parked amount. Reduce `collection_outstanding` by the exact
  applied inclusive amount on first accept only. Remaining outstanding is
  not stored on the application row. Remaining zero does not settle; #46
  remains the explicit settle-when-zero command. Do not reuse `settled` for
  voided cases. The parked leftover row stays `parked`.
- A leftover-apply row fail-closes unused invoice void after restart.
- Keep `GET /v1/unapplied-cash-applications/{id}` and list presentment
  unchanged. Reads that already work in-memory keep working when the row is
  loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist leftover refund, evaluation snapshots, statutory numbers,
  VAT/NTS, `retained_earnings` / 310100, tenant auto-create, journals, AIS
  receipts, or dimension-scoped budgets. Leftover-refund lookup stays a
  later-slice stub so leftover-apply can fail-close when a refund row exists.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

An applied leftover now survives process restart as commercial truth. GET
presentment continues to project that stored row plus current remaining
outstanding. Unused-invoice void fail-closes on a real leftover-apply row
after restart. Leftover-refund persist remains a later #84 slice. Issue #84
remains open for the other authoritative records and production
recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
