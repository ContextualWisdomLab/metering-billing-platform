# Validation

Operator and contributor validation for this repository. The customer-facing
start page is the root [README](../../README.md). Contributor policy lives in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Environment

Python 3.13+ and a dedicated PostgreSQL 18 instance are required. The unit
suite refuses to run unless `METERING_BILLING_POSTGRES_DSN` contains the
substring `test` — always point it at a throwaway database.

```bash
uv sync
export METERING_BILLING_POSTGRES_DSN="postgresql:///metering_billing_test?host=/tmp&port=5433"
```

## Apply migrations

```bash
python3 scripts/migrate_postgres.py --dsn "$METERING_BILLING_POSTGRES_DSN"
```

The runner takes one session-level advisory lock, records a checksum per
applied migration, and fails closed on drift. A valid run prints
`applied N PostgreSQL migrations` (or `applied 0` on an up-to-date database).

## Unit suite with complete branch coverage

```bash
python3 -m coverage run --branch --source=scripts,metering_billing \
  -m unittest discover -s tests -v
python3 -m coverage report --fail-under=100 --show-missing
```

Repository tooling must report 100% statement and branch coverage for the
`scripts` and `metering_billing` sources; CI enforces the same gate.

## Repository contracts

```bash
python3 scripts/validate_repository.py .
```

A valid tree prints `repository contracts valid:` and the resolved root.
Failures print one diagnostic per line: missing required files, schema
identity errors, SQL naming violations, unresolved placeholders, or mutable
GitHub Action references.

## Compile check

```bash
python3 -m compileall -q scripts tests metering_billing
```

## Exact-head CI reminder

Checkout must use the pull-request or push head, and every GitHub Action is
commit-pinned. See
[CONTRIBUTING.md → Exact-head CI](../CONTRIBUTING.md#exact-head-ci).
