# ADR 0097: Durable PostgreSQL Issued Credit-Note Void

**Status:** Accepted

## Context

#72 records one append-only commercial `issued_credit_note_void` for an unused
same-tenant issued credit note. #73 enqueues `credit_note.voided` on the
existing #24 outbox. GET presentment already projects that void.
`issued_credit_note` already reloads from `PostgresUsageLedger`. The unused
void itself still had only the `MemoryUsageLedger` path. A successful
in-process void therefore did not prove that a restart preserved the
buyer-visible unused-credit void.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified unused issued-credit-note void. Issued-invoice
snapshots already reload. Spend-budget publish, evaluation, over/approaching
signals, and #85 atomic authorization stay unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_issued_credit_note_void`, `get_issued_credit_note_void`,
  `insert_issued_credit_note_void`, and
  `list_issued_credit_note_voids_for_tenant`.
- Persist one recorded void per successful unused-note void:
  `tenant_account_id`, `issued_credit_note_id`, `credit_adjustment_id`,
  `invoice_draft_id`, optional `issued_invoice_id`,
  `issued_credit_note_void_id`, exact `voided_amount`, `voided_at`,
  `issued_credit_note_void_status`, source hash, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and issued credit note is `duplicate_replay` and
  does not insert a second row. A crash after insert and before outbox
  enqueue is healed by the next replay. Rejected void writes zero rows.
- Fail closed when the note has already been applied. The ledger looks up
  existing `credit_note_application` rows; this slice does not persist
  applications.
- Commit the void row and the existing `credit_note.voided` outbox event in
  one repository transaction.
- Keep `GET /v1/issued-credit-note-voids/{id}`, list presentment, and the #24
  outbox contracts unchanged. Reads that already work in-memory keep working
  when the row is loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, credit-note lines, statutory numbers,
  VAT/NTS, `retained_earnings` / 310100, tenant auto-create, journals, AIS
  receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This slice
  is not a claim that the HTTP default, RLS, readiness, backup/restore, HA,
  capacity benchmark, or OpenTelemetry requirements are complete. Tenant API
  credentials stay memory-only, so restart proof is the presentment services
  loaded from PostgreSQL rather than `create_http_app` on that ledger.

## Consequences

An unused issued-credit-note void now survives process restart as commercial
truth. GET presentment continues to project that stored row. Issue #84 remains
open for the other authoritative records and production recovery/deployment
controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
