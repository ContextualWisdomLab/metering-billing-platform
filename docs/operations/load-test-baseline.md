# Load Test Baseline (Compose + k6)

Every number in this file comes from a real `k6 run compose/k6/e2e_smoke.js`
execution against a healthy `metering-billing-platform` Compose stack.  Never
append unmeasured figures here; re-run the scenario and add a dated section
(ADR 0124).

## Baseline 2026-08-26 — first recorded baseline

- **Date:** 2026-08-26
- **Repository state:** branch `feat/compose-deployment-k6-baseline` on top of
  `origin/develop` commit `7792752` (deployment changeset present in the
  working tree at run time)
- **Machine context:** Apple Silicon macOS (arm64); Podman 5.8.2 machine
  (`applehv`, 4 vCPU / 8 GiB) exposing the Docker-compatible API used by Docker
  Compose v2; k6 v2.0.0 (darwin/arm64) running from the host; PostgreSQL 18 and
  the billing API as containers on one project network
- **Stack:** `docker-compose up -d --wait` reached healthy: PostgreSQL 18 →
  one-shot migration service applied 39 migrations → API healthy with
  `GET /readyz = 200 {"status": "ready", "backend": "postgres"}`
- **Seeding:** idempotent `compose/k6/seed.py` registered one demo tenant,
  billing account, principal, credential record, credential assignment, and
  meter definition; k6 `setup()` then issued the runner API key over HTTP

### Scenario

| Setting | Value |
|---|---|
| Executor | `ramping-vus` |
| Ramp | 0 → 50 VUs over 60 s |
| Sustain | 50 VUs for 60 s |
| Wall time | ~121 s |
| Requests per iteration | `GET /readyz`, authenticated `GET /v1/tenant-api-credentials`, plus `GET /healthz` on ~1-in-20 iterations |

### Results (measured)

Aggregate: **17,373 requests, 143.50 req/s, 8,476 iterations (70.01/s),
checks 100% (17,373 / 17,373), HTTP failures 0%.**

| Request kind | Share of traffic | RPS achieved | p50 (med) | p90 | p95 | max |
|---|---|---|---|---|---|---|
| `GET /readyz` | 1 per iteration (~70/s) | ~70.0 | 113 ms | 377 ms | 628 ms | 2,647 ms |
| Authenticated tenant read `GET /v1/tenant-api-credentials` | 1 per iteration (~70/s) | ~70.0 | 237 ms | 694 ms | 974 ms | 3,938 ms |
| `GET /healthz` (low weight) | ~5% of iterations (~3.5/s) | ~3.5 | 5.4 ms | 38 ms | 147 ms | 2,117 ms |
| All requests combined | 100% | 143.50 | 157 ms | 563 ms | 872 ms | 3,930 ms |

### Bottleneck observations

- The durable backend serves every operation through **one shared psycopg
  connection** guarded by a reentrant lock, so all database work serializes.
  At the 50-VU peak this is the dominant latency source: the tenant read's
  median (237 ms) is roughly double `/readyz`'s (113 ms) because each read
  pays authorization plus presentment database work behind the same queue.
- Tail latency scales with queue depth, not query cost: p95 values are 3–4×
  medians while individual queries stay cheap, and maxima near 4 s are queued
  request time rather than slow SQL.
- `/readyz` shares that same serialized connection, so its latency couples to
  read traffic; a saturated tier could delay readiness probes even though the
  process itself is alive.
- `GET /healthz` stays fast at median (static reply, no database touch) but
  still shows multi-second maxima because its replies also queue behind the
  single accept-and-process loop of the stdlib WSGI server under 50 VUs.

### Next actions

1. Introduce per-request database sessions (connection pool or session-per-
   request wiring) so reads stop queueing behind one shared connection.
2. Re-run this exact scenario after the pooling change and append a dated
   section comparing p50/p95 per request kind.
3. Move per-request access logging behind an environment flag to remove
   synchronous stderr writes from the hot path.
4. Add k6 thresholds (sanity ceilings on p95 and error rate) once numbers
   stabilize across hardware, keeping this first baseline threshold-free for
   comparability.
5. Re-baseline on target production hardware before any capacity commitment;
   these Apple-Silicon-container numbers are not transferable sizing data.

## Method contract

- Always start from a healthy stack (`--wait`) and seed before issuing load.
- Checks stay status-only sanity assertions; no performance thresholds in the
  scenario file so runs stay comparable.
- After measuring, remove the isolated stack including volumes
  (`docker compose down -v --remove-orphans`) so no test containers linger.
