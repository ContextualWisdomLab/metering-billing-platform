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
  same AIS `receipt_id` with the same `source_payload_hash` under the same
  tenant-scoped `idempotency_key`. Reusing a receipt under another key is a
  conflict, matching the memory adapter and pull service. Every other
  collision raises `ValueError` and writes nothing, so observations stay
  append-only and immutable.
- Both ledgers call one shared `validate_posting_receipt_observation` before
  any write. It requires UUID identifiers, strings in required text fields,
  and strings or `None` in optional text fields. It refuses non-integer values
  (including booleans), integers outside PostgreSQL's range, a
  `receipt_contract_version` below 1 or a negative `line_count`, a payload hash
  that does not match the stored check, text containing NUL, and lone Unicode
  surrogates that cannot be encoded as UTF-8.
  `observed_at` requires a valid Gregorian `YYYY-MM-DD` date, `T` or space,
  `HH:MM:SS`, optional one-to-six fractional digits, and `Z` or a signed
  `HH:MM` offset whose hour is at most 15 and minute at most 59. Arbitrary
  separators and finer precision are rejected rather than passed to the driver.
  The instant must remain within years 1 through 9999 after UTC conversion.
  PostgreSQL selects `observed_at AT TIME ZONE 'UTC'` before driver decoding,
  so session timezones cannot move a valid boundary instant out of range.
  PostgreSQL reads normalize offsets to UTC `Z`; direct memory writes retain
  the supplied valid timestamp string. It raises
  `PostingReceiptObservationInvalid`, a `ValueError`, so a pull reports
  `receipt_invalid` and no driver error escapes for these rejected values.
  A read key containing NUL or an unencodable surrogate returns no row,
  as the memory adapter does.
- `posting_status_code` must stay one of the AIS-owned values `posted`,
  `held`, `rejected`, or `reversed`. It is never mapped onto Billing
  `proposal_status`.
- AIS `receipt_id` stays an external reference. The internal primary key is
  `posting_receipt_observation_id`.
- `observed_at` is projected in UTC before driver decoding and returned as a
  UTC `Z` string. The normal pull path generates that same canonical string;
  direct memory writes retain a supplied valid offset string.

## Consequences

- Posting-receipt pulls, item and list reads, and AIS outbox drains work under
  the PostgreSQL backend and survive a process restart.
- A drain that finds a stored observation publishes the AIS outbox event
  without a second receipt GET.
- No migration, dependency, secret, provider call, AIS posting, or
  `proposal_status` change is introduced.
- Concurrency, backup and restore, partitioning, and the psycopg licence
  replacement (#176) remain open under issue #84.
