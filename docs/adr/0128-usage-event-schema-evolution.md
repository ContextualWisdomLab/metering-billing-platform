# ADR 0128: Usage-event schema evolution boundary

## Status

Accepted for the current usage-event v1 contract.

## Decision

Keep `schemas/usage-event.schema.json` closed and pin
`event_contract_version` to `1`. Existing optional attribution and operation
fields are additive-compatible when their meaning, hash treatment, and
persisted model remain unchanged. A semantic breaking change must publish a
new schema and contract version; the current runtime rejects that version as
`schema_invalid` until its schema, SDKs, and persistence behavior are
implemented together.

## Consequences

- Existing v1 producers continue to validate with or without optional fields.
- Unknown versions cannot reach hash verification, attribution, or persistence.
- A future version must be introduced as a separately reviewed schema and
  conformance fixture rather than widening v1 to accept unimplemented data.
- Rejected version attempts still produce the normal append-only ingestion
  receipt without storing usage or revealing payload content.
