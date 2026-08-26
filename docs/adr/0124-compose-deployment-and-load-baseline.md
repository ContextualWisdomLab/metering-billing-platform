# ADR 0124: Compose Deployment Surface and k6 Load Baseline

**Status:** Accepted

## Context

The HTTP accept surface already selects its ledger backend from the
environment (ADR 0123) and `python -m metering_billing.http_app` serves on
`0.0.0.0:$PORT`, but the repository shipped no deployment artifact and no
measured performance baseline.  Operators who wanted to try the durable
PostgreSQL path had to hand-wire a database, apply migrations, export two
environment variables, and start the process themselves.  The standalone
server also used `wsgiref`'s single-threaded request loop, so one slow
client serialized every other tenant's request behind it.

Two gaps follow from that state.  First, there was no repeatable way to boot
the whole platform — database, migrations, API — as one unit, which blocks
the future Kubernetes migration because manifests would have nothing tested
to mirror.  Second, every latency claim about `/readyz` or tenant reads was
unmeasured; a load baseline must come from a real run against the real
deployment surface, never invented.

## Decision

- Ship one root `Dockerfile` on `python:3.13-slim`.  It installs only the
  hash-locked runtime dependency set with CI's exact pip flags
  (`--disable-pip-version-check --only-binary=:all: --require-hashes -r
  requirements-runtime.txt`), copies the package, migration runner, SQL
  migrations, and seed helper, drops to a non-root user, exposes port 8000,
  and starts `python -m metering_billing.http_app`.  No multi-stage build is
  needed for a stdlib-plus-psycopg application.
- Make the web tier multithreaded: `main()` now serves through a new
  module-level `ThreadingWSGIServer` (`ThreadingMixIn` over the stdlib
  `WSGIServer`) with daemon threads, so concurrent tenant requests are
  handled in parallel.  Everything else about the entrypoint stays identical,
  including `PORT` handling with the 8000 default.
- Add `compose/docker-compose.yml` with fixed project name
  `metering-billing-platform` and three services:
  `postgres_database` (PostgreSQL 18, `pg_isready` healthcheck, named data
  volume, host port `${POSTGRES_HOST_PORT:-5433}`),
  `schema_migration` (one-shot runner of `scripts/migrate_postgres.py`
  through the advisory-locked path, `restart: "no"`, gating the API), and
  `billing_api` (built image running with
  `METERING_BILLING_LEDGER_BACKEND=postgres`, DSN pointed at the project
  service, and a stdlib `urllib` `/readyz` healthcheck so the image needs no
  curl).  First boot therefore applies migrations idempotently before the
  first request is served.
- Document every variable in `compose/.env.example` with dev-safe defaults
  and production-change guidance; Compose reads that file as `.env`.
- Record an end-to-end k6 baseline in `compose/k6/e2e_smoke.js`: ramp to a
  fixed peak of 50 virtual users over 60 seconds, sustain for 60 seconds,
  check only status correctness, and trend per-request-kind durations
  (`GET /healthz` at low weight, `GET /readyz`, and one authenticated tenant
  read against a seeded tenant whose catalog rows are registered by the
  idempotent `compose/k6/seed.py`).  The measured result of each real run is
  recorded in `docs/operations/load-test-baseline.md`; isolated test
  containers and volumes are removed after each run.
- Compose is the deployment surface of record today and the template for the
  future Kubernetes migration: service names, healthchecks, environment
  variables, and startup ordering are chosen to map one-to-one onto pods,
  init containers (migration job), and readiness probes.

## Consequences

- One command boots the full platform and `--wait` returns only when the API
  reports the PostgreSQL backend ready, so operators get a truthful
  start-to-ready signal without reading source code.
- The threaded server removes head-of-line blocking between tenants while
  keeping behavior byte-for-byte otherwise; existing entrypoint tests pin the
  server class so a future regression back to serial serving fails CI.
- The image cannot drift from the lockfile: hash verification fails the build
  if any wheel hash changes, and `--only-binary=:all:` forbids accidental
  source builds.
- Load numbers now have a home and a method.  Future capacity claims must
  re-run `compose/k6/e2e_smoke.js` against a healthy stack and append a dated
  section to `docs/operations/load-test-baseline.md` instead of asserting
  unmeasured figures.
- The baseline is a local Apple Silicon Docker run, not a production
  capacity certification; absolute numbers will differ on other hardware and
  must be re-baselined before sizing decisions.
- No new Python dependency, schema change, secret, or provider call is
  introduced; exact-decimal money and journal boundaries stay unchanged.
