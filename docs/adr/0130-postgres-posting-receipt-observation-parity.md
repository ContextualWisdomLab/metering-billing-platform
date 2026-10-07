# ADR 0130: PostgreSQL Parity for AIS Posting-Receipt Observations

**Status:** Accepted

## Context

ADR 0123 lets an operator select `PostgresUsageLedger` as the production
system of record with `METERING_BILLING_LEDGER_BACKEND=postgres`. The
`billing_core.posting_receipt_observation` table already exists in the
migration set, but `PostgresUsageLedger` did not implement the four
repository methods that `PostingReceiptPullService`,
`AisOutboxDrainService`, and `PostingReceiptObservationPresentmentService`
call. Under the PostgreSQL backend, every posting-receipt pull, read, list,
and AIS outbox drain failed with `AttributeError`. It was the only public
`MemoryUsageLedger` method family with no PostgreSQL implementation (issue
#84).

## Decision

- Add `find_posting_receipt_observation`,
  `find_posting_receipt_observation_by_receipt`,
  `insert_posting_receipt_observation`, and
  `list_posting_receipt_observations` to `PostgresUsageLedger` with the same
  contracts as the memory reference adapter.
- Every tenant-pinned query filters on `tenant_account_id`. The optional-tenant
  list uses one constant parameterized query and orders by `observed_at`, then
  `posting_receipt_observation_id`. Like the memory adapter, an explicit
  `None` tenant lists every tenant; no request path passes `None`.
- Insert uses `ON CONFLICT DO NOTHING` and then re-reads the tenant
  idempotency-key row. A collision returns the stored row only when it is the
  same AIS `receipt_id` with the same `source_payload_hash`. Every other
  collision raises `ValueError` and writes nothing, so observations stay
  append-only and immutable.
- Both ledgers call one shared `validate_posting_receipt_observation` before
  any write. It refuses an `integer` outside PostgreSQL's range, a
  `receipt_contract_version` below 1 or a negative `line_count`, a payload hash
  that does not match the stored check, an invalid timezone-aware `observed_at`,
  and text containing NUL. It raises
  `PostingReceiptObservationInvalid`, a `ValueError`, so a pull reports
  `receipt_invalid` and no driver error escapes. A read with a NUL idempotency
  key returns no row, as the memory adapter does.
- `posting_status_code` must stay one of the AIS-owned values `posted`,
  `held`, `rejected`, or `reversed`. It is never mapped onto Billing
  `proposal_status`.
- AIS `receipt_id` stays an external reference. The internal primary key is
  `posting_receipt_observation_id`.
- `observed_at` is decoded to the same UTC `Z` string that the memory adapter
  stores.

## Consequences

- Posting-receipt pulls, item and list reads, and AIS outbox drains work under
  the PostgreSQL backend and survive a process restart.
- A drain that finds a stored observation publishes the AIS outbox event
  without a second receipt GET.
- No migration, dependency, secret, provider call, AIS posting, or
  `proposal_status` change is introduced.
- Concurrency, backup and restore, partitioning, and the psycopg licence
  replacement (#176) remain open under issue #84.
