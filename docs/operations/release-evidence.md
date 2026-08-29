# Release evidence and exact-head source manifest

**Status:** deterministic source-hash evidence is executable; GA release
evidence remains incomplete until every listed external receipt is verified.

## Owner and boundary

The release owner creates and verifies evidence from the protected release
checkout. The source manifest proves only the Git HEAD, tracked-file inventory,
and SHA-256 bytes for that checkout. It is not an SPDX SBOM, SLSA provenance
statement, artifact signature, install rehearsal, backup/restore rehearsal, or
availability/performance certification.

## Create and verify

Run from the exact release checkout with a semantic version selected by the
release owner:

```sh
umask 077
uv run python scripts/release_evidence.py manifest \
  --root . \
  --release-version 0.3.0-rc.1 \
  --output /secure/release-evidence/source-manifest.json
uv run python scripts/release_evidence.py verify \
  --root . \
  --manifest /secure/release-evidence/source-manifest.json
```

Creation refuses to overwrite an existing manifest. Verification fails closed
when HEAD changes, a tracked file is missing or added, a manifest path or hash
is malformed, or any current artifact bytes differ. Preserve the manifest,
exact source SHA, command exit statuses, and the release ticket together; do
not place credentials, DSNs, customer data, prompts, responses, or provider
payloads in the evidence directory.

## Required release receipts

The source manifest is one input to the release gate. The release owner must
attach current-head receipts for each applicable control before changing the
status to released:

| Control | Required receipt | Current baseline |
|---|---|---|
| API/events/schemas/SDKs/migrations/UI | compatibility matrix and consumer tests | not verified by this manifest |
| SPDX 3.0.1 | generated SBOM and independent validation | not verified |
| SLSA 1.2 | provenance statement bound to the release bytes | not verified |
| Artifact trust | signature and fresh-environment verification | not verified |
| Install/upgrade/rollback | dated rehearsal with exact versions | not verified |
| Backup/restore/export/import | measured receipt with RPO/RTO and hashes | not verified |
| Security/quality gates | protected exact-head Checks and independent review | not verified |

Until these receipts exist, the product statement remains a release
candidate, not a GA or compliance-certified service.
