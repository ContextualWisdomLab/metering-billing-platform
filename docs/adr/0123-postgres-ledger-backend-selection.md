# ADR 0123: PostgreSQL Ledger Backend Selection for the HTTP App

**Status:** Accepted

## Context

`create_http_app` wires roughly forty commercial service objects onto one
shared ledger and previously accepted only `MemoryUsageLedger`, so every
standalone HTTP process served the in-memory reference adapter regardless of
environment. Issue #84 — replace the in-memory reference ledger with a durable
PostgreSQL production path — already delivered `PostgresUsageLedger` as the
durable system of record for catalog rows, immutable usage facts, rating runs,
invoice drafts, issued snapshots, collection facts, journal proposals, and the
atomic webhook outbox, but the HTTP adapter had no way to select it.

The memory ledger remains the deterministic reference and test adapter: the
whole unit suite depends on it staying the default. Production selection must
fail closed at startup, not on the first request, when a PostgreSQL DSN is
missing. Operators also need one readiness endpoint that reports which backend
is serving without leaking exception internals, and the probe must reuse the
ledger's own connection conventions instead of opening ad-hoc psycopg
connections beside it.

## Decision

- Define `UsageLedger = MemoryUsageLedger | PostgresUsageLedger` next to the
  ledgers in `metering_billing/postgres_usage_ledger.py`. Widen
  `create_http_app(ledger=...)` to accept either implementation. Service
  constructor signatures stay unchanged; both ledgers already satisfy the same
  duck-typed repository surface.
- Add `create_default_ledger(environ=None)` to `metering_billing/http_app.py`.
  `METERING_BILLING_LEDGER_BACKEND=postgres` builds `PostgresUsageLedger`
  from `METERING_BILLING_POSTGRES_DSN` through the existing
  `PostgresUsageLedger.connect` convention.  That constructor uses the
  bounded lazy session pool defined by ADR 0125; injected connections remain
  the one-session compatibility form for tests and externally managed
  lifecycles.
  A missing or empty DSN raises a `ValueError` naming the variable at startup.
  Every other value, including an unset variable, returns
  `MemoryUsageLedger()`, so tests keep working unchanged.
- Add unauthenticated `GET /readyz` beside the existing `/healthz` route in
  the same dispatch style. The response is `200 {"status": "ready",
  "backend": "<memory|postgres>"}` or `503 {"status": "not_ready",
  "backend": ..., "reason": "migration_history_unavailable"}`. The postgres
  probe runs one cheap `SELECT COUNT(*)` against the migration-history table
  (`public.metering_billing_schema_migration`) through the ledger's own
  cursor and transaction conventions. Reason strings stay stable short codes;
  raw exception text never reaches the response.
- Memory stays the deterministic reference/test adapter. Postgres becomes the
  selectable production system of record. This is partial progress on #84:
  restart durability now extends to the HTTP process, while concurrency,
  failover, backup/restore, and hot-partition evidence remain open backlog.

## Consequences

- Operators select the durable backend with two environment variables and get
  a truthful readiness signal per process. A misconfigured deployment refuses
  to start instead of silently serving memory-backed writes as production.
- The full existing suite keeps passing untouched because the default path is
  byte-for-byte the previous behavior. New backend-selection tests cover both
  factories' branches and all three readiness outcomes at 100% coverage.
- `GET /healthz` stays a static liveness reply; only `/readyz` touches the
  backend. Wrong-method requests on `/readyz` follow the existing route
  convention (`422 request_invalid`).
- Tenant API credentials, evaluation snapshots, atomic spend-budget
  authorization (#85), provider adapters (#86), period close (#87), identity/
  secret/compliance hardening (#88), SDKs (#90), and operability evidence
  (#91) remain unchanged and open.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
- IEEE. (2019). *IEEE Standard for Floating-Point Arithmetic*
  (IEEE 754-2019).
- IFRS Foundation. (2024). *IFRS 15 Revenue from Contracts with Customers*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
