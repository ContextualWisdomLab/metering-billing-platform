# Producer SDK release runbook

The three producer artifacts are released only from a published GitHub
release. The protected workflow
[`publish-producer-sdks.yml`](../../.github/workflows/publish-producer-sdks.yml)
first resolves the published tag to one immutable commit, packages all
artifacts from that commit, runs each SDK's conformance tests, and publishes
them from the `producer-release` environment. The Rust publish step also
compares its re-packaged crate with the uploaded artifact before publishing.

Current package identities are:

- Python: `metering-billing-platform` (imports `metering_billing`)
- Rust: `cwl-metering-producer`
- TypeScript: `@contextualwisdomlab/metering-producer`

The hourly [`Producer smoke and replay`](../../.github/workflows/producer-smoke-replay.yml)
workflow also supports manual dispatch. It builds one count-only event through
each of the contextual-orchestrator, NewsDOM, and fast-mlsirm adapters, persists
them in the SQLite producer outbox, injects a Billing outage, reopens the
outbox, and verifies accepted delivery plus server duplicate replay. It never
stores content or credentials. This is pre-release integration evidence; the
issue #90 GA criterion still requires the released SDK pins and real producer
production/staging endpoints.

Before creating a release:

1. Bump the relevant package versions and update `CHANGELOG.md`.
2. Run `uv build --out-dir dist`, `cargo package --locked --manifest-path
   sdks/rust/Cargo.toml`, and `npm pack --dry-run` from
   `sdks/typescript`.
3. Merge the release commit through the normal protected PR gates.
4. Create and publish a GitHub release for the merged tag. The workflow does
   not run for an unpublished draft release or an arbitrary branch push.

Repository administrators must configure the `producer-release` environment,
PyPI and npm trusted publishers, and the `CARGO_REGISTRY_TOKEN` secret before
the first publish. PyPI and npm publish with OIDC provenance; the Rust publish
uses the scoped registry token. No producer payload, credential value, prompt,
response, document content, or provider identifier belongs in release metadata.

Registry versions are immutable. If one registry accepts an artifact and a
later publish step fails, fix the external configuration and do not rerun a
different artifact under the same version; bump the affected package version,
update the changelog, and publish a new protected release.
