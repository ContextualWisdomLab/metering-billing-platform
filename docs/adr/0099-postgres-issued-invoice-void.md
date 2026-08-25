# ADR 0099: Durable PostgreSQL Issued-Invoice Void

**Status:** Accepted

## Context

#63 records one append-only commercial `issued_invoice_void` for an unused
same-tenant issued invoice. #64 enqueues `invoice.voided` on the existing
#24 outbox. GET presentment already projects that void. `issued_invoice`
already reloads from `PostgresUsageLedger`. Credit-note applications,
unused issued-credit-note voids, and issued credit notes already reload.
The unused invoice void itself still had only the `MemoryUsageLedger` path.
A successful in-process void therefore did not prove that a restart
preserved the buyer-visible unused-invoice void or the case closed as
`voided`.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified unused issued-invoice void. Collection cases as
a new write, payment receipts, unapplied cash, and #85 atomic authorization
stay unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_issued_invoice_void`, `get_issued_invoice_void`,
  `insert_issued_invoice_void`, `list_issued_invoice_voids_for_tenant`,
  and `mark_collection_case_voided`.
- Persist one recorded void per successful unused-invoice void:
  `tenant_account_id`, `issued_invoice_id`, `invoice_draft_id`, optional
  `collection_case_id`, `issued_invoice_void_id`, exact `voided_amount`,
  exact-zero `remaining_outstanding_amount`, `voided_at`,
  `issued_invoice_void_status`, source hash, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and issued invoice is `duplicate_replay` and
  does not insert a second row or re-close the case. A concurrent insert
  identity race classifies as `duplicate_replay` when insert returns the
  stored id. A crash after insert and before outbox enqueue is healed by
  the next replay. Rejected void writes zero rows.
- First accept closes an unused open or dunning case as `voided` at
  exact-zero remaining. Do not reuse `settled`. Fail closed when the case
  has a payment receipt, credit-note application, write-off, leftover
  apply, or remaining that is not the issued inclusive amount.
- Leftover-apply persist remains a later #84 slice. The unused-void path
  still looks up `unapplied_cash_application` so a leftover-apply row
  fail-closes; this slice does not persist leftover apply.
- Keep `GET /v1/issued-invoice-voids/{id}`, list presentment, and the #24
  outbox contracts unchanged. Reads that already work in-memory keep working
  when the row is loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, invoice lines, statutory numbers,
  VAT/NTS, `retained_earnings` / 310100, tenant auto-create, journals, AIS
  receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

An unused issued-invoice void now survives process restart as commercial
truth. GET presentment continues to project that stored row plus the current
voided case remaining. Issue #84 remains open for the other authoritative
records and production recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
