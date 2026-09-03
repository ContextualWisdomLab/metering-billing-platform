# ADR 0128: Hash-backed reconciliation evidence

## Status

Accepted for the next implementation slice of issue #87.

## Decision

Persist each source-evidence reference as an immutable
`reconciliation_evidence` fact linked to an existing reconciliation exception.
The fact records the evidence kind, source reference, SHA-256 content digest,
capturing operator, and capture instant. Multiple evidence facts may support
one exception; an evidence identifier can only replay its original content.

The repository does not fetch provider documents or store their payloads in
this slice. A later reconciliation run can require evidence for each blocking
exception and use the digest to detect changed source content.

## Consequences

- Exception evidence has a durable, tenant-scoped lookup path and provenance hash.
- Evidence cannot be attached to a line unless that typed exception already exists.
- Provider connectors, run completeness, aging, and payload archival remain follow-up work.
