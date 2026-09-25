# ADR 0125: Commercially Compatible PostgreSQL Client

**Status:** Accepted

## Context

`PostgresUsageLedger`, `scripts/migrate_postgres.py`, unauthenticated
`GET /readyz`, and the environment-selected PostgreSQL ledger (ADR 0123 /
ADR 0124) already persist the durable commercial facts. The runtime
dependency that opened those sessions was LGPL `psycopg[binary]`. A
commercial distribution cannot ship that client, take a license exception,
or document a GPL/LGPL/AGPL install path.

The ledger and migration runner depend on a small connection surface:
libpq-style DSN parsing (keyword, URI, and unix-socket forms),
`connection.execute`, `connection.cursor`, nested `connection.transaction`,
and a stable foreign-key error type. Rewriting the durable ledger onto an
async driver, or binding `libpq` through `ctypes`, would change that
surface without changing any commercial fact.

Issue #176 requires the replacement to keep Exact Decimal money, 3NF
migrations, the CSAP/SOC2 posture (no PAN, CVC, or provider secrets), and
the Billing/AIS boundary. It does not implement #85, VAT/NTS,
`retained_earnings` / 310100, or invoice-void Storybook work. After this
decision lands, public README and operator docs can claim a license-clean
PostgreSQL client for issue #157.

## Decision

- Replace the runtime PostgreSQL client with BSD-3-Clause `pg8000`
  (`pg8000>=1.31,<2`), a pure-Python DB-API driver. Install it as a
  hash-locked wheel with `--only-binary=:all:`. The locked tree is
  `pg8000`, MIT `scramp` and `asn1crypto`, Apache-2.0 / BSD
  `python-dateutil`, and MIT `six`.
- Expose the existing ledger/migration session surface through
  `metering_billing.postgres_client`. `PostgresUsageLedger.connect` and
  `scripts/migrate_postgres.py` open sessions only through that adapter.
  Integration tests use the same `connect` / `ForeignKeyViolation` types.
- Parse the existing CI, Compose, and local DSNs: keyword
  (`dbname=... user=... host=127.0.0.1`), URI
  (`postgresql://user:pass@host:5432/db`), and unix-socket
  (`postgresql:///db?host=/tmp&port=5433`). Map libpq `sslmode` onto
  `pg8000.dbapi.connect(ssl_context=...)`: `disable` / `allow` omit TLS;
  `prefer` / `require` open TLS without certificate verification;
  `verify-ca` verifies the CA without a hostname check; `verify-full`
  uses the default verifying context. Unknown modes fail closed.
  `allow` and `prefer` do not implement libpq's fallback negotiation.
- Split unparameterized migration bodies on top-level semicolons so
  `apply_migrations` can still `connection.execute` a multi-statement
  script. Nested `transaction()` blocks use savepoints. Depth increment
  and `SAVEPOINT` share the `finally` that restores depth so a refused
  savepoint cannot leak nesting.
- Read SQLSTATE from `error.sqlstate` or the pg8000 `DatabaseError`
  payload `args[0]["C"]`. When a SQLSTATE is present, only `23503` maps
  onto `metering_billing.postgres_client.ForeignKeyViolation`. Message
  text is a fallback only when no SQLSTATE is present.
- Reject copyleft PostgreSQL clients case-insensitively, including
  `psycopg3`, `psycopg-c`, and `psycopg_binary`, in `pyproject.toml`,
  `requirements-runtime.txt`, and `uv.lock`. `uv.lock` must also name
  `pg8000`. Public operator docs (README, CHANGELOG, ARCHITECTURE,
  CONTRIBUTING, VALIDATION) must not present the replaced client as an
  acceptable commercial install. README must name `pg8000` and claim
  the durable client is license-clean.
- Record the locked commercially compatible dependency-tree licenses:
  `pg8000` BSD-3-Clause, `scramp` MIT, `asn1crypto` MIT,
  `python-dateutil` Apache-2.0 / BSD, and `six` MIT. Do not add a
  GPL/LGPL/AGPL client to that tree.

## Consequences

- Durable ledger contracts stay the same: tenant-scoped facts, exact
  decimal amounts, advisory-locked migrations, `/readyz` through the
  ledger's own cursor, and environment-selected `PostgresUsageLedger`.
- The HTTP image and CI install one commercially compatible wheel set
  instead of an LGPL binary. Compose, Dockerfile `PYTHONPATH`, and the
  hash-lock flags do not change.
- Operators and #157 public docs can state that the PostgreSQL runtime is
  license-clean. ADR text may still name the rejected client so the
  replacement is reviewable.
- Connection pooling, failover, backup/restore, and #85 atomic
  authorization remain open under issue #84. This ADR does not post
  journals, call AIS, or change commercial money facts.

## References

- Helland, P. (2012). *Idempotence is not a medical condition*.
- PostgreSQL Global Development Group. (2026). *INSERT*.
- PostgreSQL Global Development Group. (2026). *Constraints*.
- PCI Security Standards Council. (2024). *PCI DSS 4.0.1*.
