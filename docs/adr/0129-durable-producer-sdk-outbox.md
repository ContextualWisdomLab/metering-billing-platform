# ADR 0129: Durable producer SDK outbox and receipt delivery

## Context

The canonical Python, Rust, and TypeScript builders produce the same
CloudEvents-compatible usage fact, but a producer process can still lose that
fact between construction and HTTP delivery. The existing
`POST /v1/usage-events` endpoint already returns ordered per-event receipts,
including accepted, duplicate replay, and rejected outcomes.

## Decision

Add a small durable outbox to each SDK:

- Python uses SQLite; Rust and TypeScript use an atomically replaced local
  file, with no new runtime dependency.
- enqueue validates the closed event and rejects a reused `event_id` whose
  bytes differ; it never generates or mutates an event identifier;
- a bounded flush sends at most the configured batch size;
- only a receipt whose tenant, source key, contract version, and source hash
  match the queued event removes it;
- accepted and duplicate-replay receipts are acknowledged, rejected receipts
  are retained as dead letters, and missing/malformed receipts or transient
  transport failures remain pending until the bounded attempt limit;
- dead letters require explicit replay; the SDKs provide optional HTTPS
  senders, while endpoint selection, credential values, and scheduling remain
  application-owned.

The reference tests cover both accepted and duplicate acknowledgements,
matched dead-letter rejection, stale or tenant-mismatched receipts, bounded
retry exhaustion, and durable rename behavior. Rust's CI coverage report also
keeps the producer implementation on a pinned coverage toolchain; platform
specific write and directory-sync faults remain reported as fault-injection
work rather than being treated as ordinary contract-path coverage.

The server remains the monetary-effect authority. A crash after server accept
and before local deletion causes an at-least-once replay, while the server's
tenant-scoped idempotency prevents a second usage fact. Telemetry and tracing
may correlate a delivery attempt, but sampled telemetry is not ledger truth.
The server persists the producer contract version, meter version, bounded
repository and trace references, availability time, and correction lineage as
append-only event metadata; `recorded_at` remains server-assigned.

Contract evolution is fail-closed: the current closed schema publishes only
`event_contract_version=1`. Additive changes must publish a new schema `$id`
and contract version while keeping the v1 payload valid; breaking changes must
use a new event contract version and never reinterpret or remove a v1 field.
Until that schema is published and added to the supported-version registry,
the SDK and ingestion validator reject the version before enqueue or storage.

Release artifacts are built only from a published GitHub release tag. The
repository workflow resolves that tag to one commit before any checkout,
packages the Python distribution, Rust crate, and TypeScript tarball from that
commit, and compares the uploaded Rust crate with a fresh package from the
same commit before publishing behind the `producer-release` environment. PyPI
and npm use OIDC provenance; crates.io uses the scoped
`CARGO_REGISTRY_TOKEN` secret. Package versions must be bumped with the
changelog before a later release because registry versions are immutable.

## Consequences

The three SDKs now provide a real local outage buffer and partial-receipt
boundary. The file implementations are process-local and rewrite the small
queue atomically; a high-throughput multi-process queue, remote broker, and
full retry scheduler remain deployment concerns. Producer integrations still
need released SDK pins and real scheduled smoke/replay evidence before issue
#90 is complete.

## References

Cloud Native Computing Foundation. (2024). *CloudEvents specification*
(Version 1.0.2). https://github.com/cloudevents/spec

OpenTelemetry Authors. (n.d.). *Semantic conventions*.
https://opentelemetry.io/docs/specs/semconv/

World Wide Web Consortium. (2021). *Trace Context*.
https://www.w3.org/TR/trace-context/

Python Packaging Authority. (n.d.). *Publishing package distribution releases
using GitHub Actions CI/CD workflows*.
https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/

npm. (n.d.). *Generating provenance statements*.
https://docs.npmjs.com/generating-provenance-statements/

Rust Project. (n.d.). *Cargo publishing*.
https://doc.rust-lang.org/cargo/commands/cargo-publish.html
