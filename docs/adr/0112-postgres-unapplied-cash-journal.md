# ADR 0112: Durable PostgreSQL Leftover Journal

**Status:** Accepted

## Context

#60 composes one validated leftover `accounting_journal_proposal` from a
stored parked `unapplied_cash`. GET presentment already projects that row
through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. Parked leftover, leftover-apply,
leftover-refund, and cash/credit/write-off journals already reload from
`PostgresUsageLedger`. The leftover itself therefore survives restart, but
leftover journal compose still had only the `MemoryUsageLedger` path.
`insert_journal_proposal` stored cash, credit, and write-off identities and
omitted `unapplied_cash_id`. `find_journal_proposal_for_unapplied_cash` did
not exist. A successful in-process compose therefore did not prove that a
restart preserved the buyer-visible leftover journal.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified leftover journal money fact. Leftover-apply,
leftover-refund, unused invoice-void, unused credit-note-void, and
invoice-draft journals stay later. Tenant API credentials stay memory-only.
Evaluation snapshots and #85 atomic authorization stay later.

Helland (2012) requires replay to acknowledge the stored fact rather than
insert a second row. IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider
secrets off this path (PCI Security Standards Council, 2024). PostgreSQL
18 documents `uuidv7()` and `ON CONFLICT DO NOTHING` for identity and
concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_journal_proposal_for_unapplied_cash`. Persist `unapplied_cash_id`
  on `insert_journal_proposal` and replay the leftover identity through
  the existing unique index.
- Persist one validated leftover journal per successful compose:
  `tenant_account_id`, `unapplied_cash_id`, `invoice_draft_id`,
  `journal_proposal_id`, exact `cash_receipt` debit and `unapplied_cash`
  credit, `proposed_at`, `proposal_status=validated`, source hash, and
  contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains
  the table default.
- Replay of the same tenant and parked leftover is `duplicate_replay` and
  does not insert a second row or mutate remaining. A concurrent insert
  identity race classifies as the stored proposal when insert returns the
  stored id. A crash after insert and before the existing
  `journal_proposal.validated` outbox enqueue is healed by the next replay.
  Rejected compose writes zero journal rows.
- Keep leftover `unapplied_cash_status=parked`. Do not apply, refund,
  settle, or void. Do not compose a new journal type. Do not flip
  `proposal_status` to `posted`.
- Keep `GET /v1/journal-proposals/{id}` and list presentment unchanged.
  Reads that already work in-memory keep working when the row is loaded
  from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist leftover-apply, leftover-refund, invoice-void, or
  credit-note-void journals, invoice-draft `find_journal_proposal`,
  evaluation snapshots, statutory numbers, VAT/NTS,
  `retained_earnings` / 310100, tenant auto-create, AIS receipts, or
  dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This
  slice is not a claim that the HTTP default, RLS, readiness,
  backup/restore, HA, capacity benchmark, or OpenTelemetry requirements
  are complete. Tenant API credentials stay memory-only, so restart proof
  is the presentment services loaded from PostgreSQL rather than
  `create_http_app` on that ledger.

## Consequences

A leftover journal now survives process restart as commercial truth.
GET presentment continues to project that stored row with exact leftover
cash and unapplied-cash lines. Parked leftover stays parked. Replay heals
insert-then-outbox crash windows without inserting a second row. Issue
#84 remains open for the other authoritative records and production
recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
