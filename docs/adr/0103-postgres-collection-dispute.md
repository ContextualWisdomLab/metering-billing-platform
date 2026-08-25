# ADR 0103: Durable PostgreSQL Collection Dispute

**Status:** Accepted

## Context

#66 holds one unused open or dunning collection case as an append-only
commercial `collection_dispute`. GET presentment already projects that
row plus current remaining and case status. Collection cases, write-offs,
and leftover refund already reload from `PostgresUsageLedger`. The case
itself therefore survives restart, but collection-dispute hold and
in-place release still had only the `MemoryUsageLedger` path. A
successful in-process hold therefore did not prove that a restart
preserved the buyer-visible hold or that leftover-apply, write-off,
settle-when-zero, and void could fail-close on a real disputed case.

Hold insert and `mark_collection_case_disputed` are separate
transactions. A crash after insert and before the status flip left a
held row with the case still `open` or `dunning`; replay returned
`duplicate_replay` without flipping. Release has the same window after
`mark_collection_dispute_released` and before
`mark_collection_case_released_from_dispute`. That mirrors the #110
unused-void and #113 leftover-apply remaining heals.

Issue #84 remains the broader durable-runtime backlog. This slice persists
only the already-specified collection-dispute money fact, then heals hold
and release status replay. Journals and #85 atomic authorization stay
unchanged. Tenant API credentials stay memory-only.

Helland (2012) requires replay to acknowledge the stored fact rather than
mutate remaining. IEEE 754 forbids smuggling binary floating-point values
into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider
secrets off this path (PCI Security Standards Council, 2024). PostgreSQL
18 documents `uuidv7()` and `ON CONFLICT DO NOTHING` for identity and
concurrent writes.

## Decision

- Extend `PostgresUsageLedger` with tenant-scoped
  `find_collection_dispute`, `get_collection_dispute`,
  `insert_collection_dispute`,
  `list_collection_disputes_for_tenant`,
  `mark_collection_dispute_released`, `mark_collection_case_disputed`,
  and `mark_collection_case_released_from_dispute`.
- Persist one held row per successful hold: `tenant_account_id`,
  `collection_case_id`, `invoice_draft_id`, optional `issued_invoice_id`,
  `collection_dispute_id`, exact `remaining_outstanding_amount` snapshot,
  `held_at`, `collection_dispute_status=held`, source hash, and contract
  version.
- Keep tenant-scoped composite foreign keys and exact `numeric` amounts.
  Application identifiers stay UUIDv7; PostgreSQL 18 `uuidv7()` remains
  the table default.
- Replay of the same tenant and collection case is `duplicate_replay`
  and does not insert a second row or mutate remaining. A concurrent
  insert identity race classifies as `duplicate_replay` when insert
  returns the stored id. A crash after insert and before the existing
  `dispute.held` outbox enqueue is healed by the next replay. Rejected
  hold writes zero rows.
- On hold `duplicate_replay`, if the case is still `open` or `dunning`,
  flip it to `disputed`. Already-disputed cases stay as-is. Do not change
  remaining. Do not settle or void.
- Release flips the same row to `released` and stores `released_at`.
  Replay of the same tenant and `collection_dispute_id` is
  `duplicate_replay`. A crash after the row flip and before restoring
  the case is healed by the next replay when the case is still
  `disputed`. Case status returns to `open`, or to `dunning` when stored
  notices already exist. Remaining stays unchanged.
- Keep `GET /v1/collection-disputes/{id}`,
  `GET /v1/collection-dispute-releases/{id}`, and list presentment
  unchanged. Reads that already work in-memory keep working when the row
  is loaded from PostgreSQL.
- Pin `X-CWL-Tenant-Reference` to commercial `tenant_account`. Missing or
  mismatched pins fail closed.
- Do not persist evaluation snapshots, statutory numbers, VAT/NTS,
  `retained_earnings` / 310100, tenant auto-create, journals, AIS
  receipts, or dimension-scoped budgets.
- Keep `MemoryUsageLedger` as the deterministic reference adapter. This
  slice is not a claim that the HTTP default, RLS, readiness,
  backup/restore, HA, capacity benchmark, or OpenTelemetry requirements
  are complete. Tenant API credentials stay memory-only, so restart proof
  is the presentment services loaded from PostgreSQL rather than
  `create_http_app` on that ledger.

## Consequences

A collection-dispute hold now survives process restart as commercial
truth. GET presentment continues to project that stored row plus current
remaining and case status. Write-off, leftover-apply, settle-when-zero,
and void fail-close on a real disputed case after restart. Hold and
release replay heal insert-then-status crash windows without mutating
remaining. Issue #84 remains open for the other authoritative records
and production recovery/deployment controls. Issue #85 remains later.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). [INSERT](https://www.postgresql.org/docs/18/sql-insert.html).
- PostgreSQL Global Development Group. (2026). [Constraints](https://www.postgresql.org/docs/18/ddl-constraints.html).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic* (IEEE 754-2019).
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
