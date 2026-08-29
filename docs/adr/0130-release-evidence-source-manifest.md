# ADR 0130: Exact-head release evidence source manifest

## Context

The repository can run its tests and contract validator from a checkout, but a
release claim also needs evidence that the reviewed source bytes are the bytes
being packaged. A mutable branch name or a local test result is not sufficient
release identity.

## Decision

Add a standard-library `scripts/release_evidence.py` command that records the
full Git `HEAD`, semantic release version, every tracked-file path, and its
SHA-256 digest. Verification recomputes the current inventory and bytes and
fails closed on a changed commit, missing/extra file, malformed manifest, or
hash mismatch. The command refuses to overwrite an existing manifest.

The manifest has a versioned JSON Schema. It deliberately does not claim SPDX
SBOM, SLSA provenance, signatures, install/upgrade evidence, backup/restore
RPO/RTO, or certification; those remain separate release receipts.

## Consequences

Release automation has one deterministic source-identity input that can be
attached to a ticket and checked in a fresh checkout. The manifest hashes
tracked source only and does not read secrets or runtime payloads. A complete
release still requires the external artifact, operational, security, and
protected-PR receipts documented in the release procedure.
