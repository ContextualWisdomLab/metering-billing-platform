# ADR 0100: Durable PostgreSQL Unapplied Cash

**Status:** Accepted

## Context

#54 parks leftover remittance as one append-only commercial `unapplied_cash`
row against a stored payment receipt. GET presentment already projects that
row. Payment receipts, collection cases, unused issued-invoice voids, and
credit-note applications already reload from `PostgresUsageLedger`. The
parked leftover itself still had only the `MemoryUsageLedger` path. A
successful in-process park therefore did not prove that a restart preserved
the buyer-visible leftover.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified parked leftover. Leftover-apply
(`unapplied_cash_application`) cannot reload until this parent row exists:
migration `0024` tenant-scopes that apply onto `unapplied_cash`. The #110
unused-void leftover-apply lookup stub stays until that later slice. Unused
invoice voids, leftover refunds, journals, and #85 atomic authorization stay
unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped `find_unapplied_cash`,
  `get_unapplied_cash`, `insert_unapplied_cash`, and
  `list_unapplied_cash_for_tenant`.
- Persist one parked row per successful leftover park: `tenant_account_id`,
  `payment_receipt_id`, `payment_intent_id`, `collection_case_id`,
  `unapplied_cash_id`, exact `unapplied_amount`, exact receipt
  `received_amount` / `applied_amount` snapshots, `parked_at`,
  `unapplied_cash_status=parked`, source hash, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and payment receipt is `duplicate_replay` and
  does not insert a second row or mutate remaining. A concurrent insert
  identity race classifies as `duplicate_replay` when insert returns the
  stored id. Park leftover writes no outbox (ADR 0051). Rejected park writes
  zero rows.
- Receipt amount and case remaining stay unchanged. #12 still rejects
  overpay. Omitting leftover fail-closes as already consumed.
- Keep `GET /v1/unapplied-cash/{id}` and list presentment unchanged. Reads
  that already work in-memory keep working when the row is loaded from
  PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist leftover-apply, leftover refund, evaluation snapshots,
  statutory numbers, VAT/NTS, `retained_earnings` / 310100, tenant
  auto-create, journals, AIS receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

A parked leftover now survives process restart as commercial truth. GET
presentment continues to project that stored row. Leftover-apply persist
remains a later #84 slice so unused-invoice void can fail-close on a real
leftover-apply row after restart. Issue #84 remains open for the other
authoritative records and production recovery/deployment controls. Issue #85
remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
