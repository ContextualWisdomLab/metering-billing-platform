# ADR 0111: Durable PostgreSQL Write-Off Journal

**Status:** Accepted

## Context

#51 composes one validated write-off `accounting_journal_proposal` from a
stored `collection_write_off`. GET presentment already projects that row
through existing `GET /v1/journal-proposals` and
`GET /v1/journal-proposals/{proposal_id}`. Collection write-off,
collection-case settlement, leftover, credit notes, and cash/credit
journals already reload from `PostgresUsageLedger`. The write-off itself
therefore survives restart, but write-off journal compose still had only
the `MemoryUsageLedger` path. `insert_journal_proposal` stored cash and
credit identities and omitted `collection_write_off_id`.
`find_journal_proposal_for_write_off` did not exist. A successful
in-process compose therefore did not prove that a restart preserved the
buyer-visible write-off journal.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified write-off journal money fact. Leftover,
refund, leftover-apply, invoice-void, and credit-note-void journals stay
later. Tenant API credentials stay memory-only. Evaluation snapshots and
#85 atomic authorization stay later.

Helland (2012) requires replay to acknowledge the stored fact rather than
insert a second row. IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider
secrets off this path (PCI Security Standards Council, 2024). PostgreSQL
18 documents `uuidv7()` and `ON CONFLICT DO NOTHING` for identity and
concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_journal_proposal_for_write_off`. Persist `collection_write_off_id`
  on `insert_journal_proposal` and replay the write-off identity through
  the existing unique index.
- Persist one validated write-off journal per successful compose:
  `tenant_account_id`, `collection_write_off_id`, `invoice_draft_id`,
  `journal_proposal_id`, exact `write_off_expense` debit and
  `accounts_receivable` credit, `proposed_at`, `proposal_status=validated`,
  source hash, and contract version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains
  the table default.
- Replay of the same tenant and collection write-off is
  `duplicate_replay` and does not insert a second row or mutate remaining.
  A concurrent insert identity race classifies as the stored proposal when
  insert returns the stored id. A crash after insert and before the
  existing `journal_proposal.validated` outbox enqueue is healed by the
  next replay. Rejected compose writes zero journal rows.
- Keep remaining exact zero after the stored write-off. Do not settle or
  void. Do not compose a new journal type. Do not flip `proposal_status`
  to `posted`.
- Keep `GET /v1/journal-proposals/{id}` and list presentment unchanged.
  Reads that already work in-memory keep working when the row is loaded
  from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist leftover, refund, leftover-apply, invoice-void, or
  credit-note-void journals, evaluation snapshots, statutory numbers,
  VAT/NTS, `retained_earnings` / 310100, tenant auto-create, AIS
  receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This
  slice is not a claim that the HTTP default, RLS, readiness,
  backup/restore, HA, capacity benchmark, or OpenTelemetry requirements
  are complete. Tenant API credentials stay memory-only, so restart proof
  is the presentment services loaded from PostgreSQL rather than
  `create_http_app` on that ledger.

## Consequences

A write-off journal now survives process restart as commercial truth.
GET presentment continues to project that stored row with exact write-off
expense and AR lines. Remaining stays exact zero. Replay heals
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
