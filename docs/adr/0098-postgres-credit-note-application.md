# ADR 0098: Durable PostgreSQL Credit-Note Application

**Status:** Accepted

## Context

#45 applies one append-only commercial `credit_note_application` onto one
open same-tenant collection case. #46 enqueues `credit_note.applied` on the
existing #24 outbox. GET presentment already projects that row plus current
remaining outstanding. `issued_credit_note` and unused
`issued_credit_note_void` already reload from `PostgresUsageLedger`. The
unused-void path looks up `credit_note_application` to fail closed, but
#108 explicitly did not persist applications. A successful in-process apply
therefore did not prove that a restart preserved the buyer-visible applied
credit.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified credit-note application. Issued-invoice voids,
collection cases as a new write, payment receipts, unapplied cash, and #85
atomic authorization stay unchanged.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate it. IEEE 754 forbids smuggling binary floating-point values into money
(IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path
(PCI Security Standards Council, 2024). PostgreSQL 18 documents `uuidv7()` and
`ON CONFLICT DO NOTHING` for identity and concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `get_credit_note_application`, `insert_credit_note_application`, and
  `list_credit_note_applications_for_tenant`. Keep the existing
  `find_credit_note_application` used by unused-void fail-closed.
- Persist one applied row per successful apply: `tenant_account_id`,
  `issued_credit_note_id`, `collection_case_id`, `invoice_draft_id`,
  optional `issued_invoice_id`, `credit_note_application_id`, exact
  `applied_amount`, `applied_at`, `credit_note_application_status`, source
  hashes, and contract versions.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains the
  table default.
- Replay of the same tenant and issued credit note is `duplicate_replay` and
  does not insert a second row or reduce outstanding again. A concurrent
  insert identity race classifies as `duplicate_replay` when insert returns
  the stored id. A crash after insert and before outbox enqueue is healed by
  the next replay. Rejected apply writes zero rows, including voided notes.
- Reduce `collection_outstanding` by the exact issued inclusive amount on
  first accept only. Remaining outstanding is not stored on the application
  row.
- Keep `GET /v1/credit-note-applications/{id}`, list presentment, and the #24
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

An applied commercial credit note now survives process restart as commercial
truth. GET presentment continues to project that stored row plus current
remaining outstanding. Issue #84 remains open for the other authoritative
records and production recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
