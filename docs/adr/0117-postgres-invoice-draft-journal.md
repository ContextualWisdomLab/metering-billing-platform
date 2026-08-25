# ADR 0117: Durable PostgreSQL Invoice-Draft Journal

**Status:** Accepted

## Context

#8 / ADR 0006 composes one validated invoice-draft
`accounting_journal_proposal` from a stored `invoice_draft`. GET
presentment already projects that row through existing
`GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
Cash, credit, write-off, leftover, leftover-apply, leftover-refund, unused
invoice-void, and unused credit-note-void journals already reload from
`PostgresUsageLedger`. Draft finders already existed to bind unused
invoice-void compose: `find_journal_proposal_for_invoice_draft` is the
void bind lookup, and `find_journal_proposal` supports the original
invoice-draft compose identity. Those finders did not make restart proof.
`insert_journal_proposal` classified cash, credit, write-off, leftover,
leftover-apply, leftover-refund, unused invoice-void, and unused
credit-note-void identity races and raised on a draft-only source. A
crash after insert and before the existing `journal_proposal.validated`
outbox enqueue was not healed by the next `propose_journal` replay. A
successful in-process compose therefore did not prove that a restart
preserved the buyer-visible invoice-draft journal.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified invoice-draft journal money fact. Tenant API
credentials stay memory-only. Evaluation snapshots and #85 atomic
authorization stay later.

Helland (2012) requires replay to acknowledge the stored fact rather than
insert a second row. IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider
secrets off this path (PCI Security Standards Council, 2024). PostgreSQL
18 documents `uuidv7()` and `ON CONFLICT DO NOTHING` for identity and
concurrent writes.

## Decision

- Keep `find_journal_proposal` as the draft-only compose lookup:
  `(tenant_account_id, invoice_draft_id, source_payload_hash,
  proposal_contract_version)` with specialized source identities null.
  `find_journal_proposal_for_invoice_draft` stays the void bind lookup.
  Classify a draft-only insert identity race as the stored proposal when
  insert returns the stored id.
- Persist one validated invoice-draft journal per successful compose:
  `tenant_account_id`, `invoice_draft_id`, `journal_proposal_id`, exact
  semantic lines, `proposed_at`, `proposal_status=validated`, source
  hash, and contract version. Untaxed lines debit `accounts_receivable`
  and credit `usage_revenue`. Taxed lines debit `accounts_receivable`
  inclusive, credit `usage_revenue` exclusive, and credit `tax_payable`
  tax.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains
  the table default.
- Replay of the same tenant, draft, source hash, and contract version is
  `duplicate_replay` and does not insert a second row. A concurrent
  insert identity race classifies as the stored proposal when insert
  returns the stored id. A crash after insert and before the existing
  `journal_proposal.validated` outbox enqueue is healed by the next
  replay. Rejected compose writes zero journal rows.
- Keep leftover-apply remaining `19.999`. Keep unused credit-note void
  `11.00`. Keep unused issued-invoice void inclusive `voided_amount`
  `110.00`. Keep cash journal debit `0.003705`. Do not re-void, apply,
  refund, or settle. Do not compose a new journal type. Do not flip
  `proposal_status` to `posted`.
- Keep `GET /v1/journal-proposals/{id}` and list presentment unchanged.
  Reads that already work in-memory keep working when the row is loaded
  from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, statutory numbers, VAT/NTS,
  `retained_earnings` / 310100, tenant auto-create, AIS receipts, or
  dimension-scoped budgets. Do not add invoice-draft journal Storybook
  in this slice.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This
  slice is not a claim that the HTTP default, RLS, readiness,
  backup/restore, HA, capacity benchmark, or OpenTelemetry requirements
  are complete. Tenant API credentials stay memory-only, so restart proof
  is the presentment services loaded from PostgreSQL rather than
  `create_http_app` on that ledger.

## Consequences

An invoice-draft journal now survives process restart as commercial
truth. GET presentment continues to project that stored row with exact
AR, revenue, and optional tax-payable lines. Leftover-apply remaining
stays `19.999`. Unused credit-note void stays `11.00`. Unused
issued-invoice void inclusive `voided_amount` stays `110.00`. Cash
journal debit stays `0.003705`. Replay heals insert-then-outbox crash
windows without inserting a second row. Already-specified journal persist
is exhausted; tenant API credentials stay memory-only. Issue #84 remains
open for the other authoritative records and production
recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
