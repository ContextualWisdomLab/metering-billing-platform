# ADR 0136: Compose a Rated Late Adjustment into an Invoice Draft

## Status

Accepted for the #87 late-period adjustment slice.

## Decision

Record a rated late adjustment as a separate immutable
`late_adjustment_invoice_adjustment` fact linked to one same-tenant invoice
draft. The fact copies the signed amount, currency, target period, application,
and rating identities and adds operator and authorization evidence.

The command is exposed at
`POST /v1/late-adjustments/{late_adjustment_id}/invoice-adjustments` and
requires `invoice_draft_id`, `recorded_by`, and
`authorization_reference`. Its identity is the tenant and rated adjustment;
replays return the stored composition. A currency mismatch, cross-tenant draft,
or already-issued draft is rejected. Existing drafts and issued invoice
snapshots are never overwritten.

## Consequences

- The signed late-adjustment delta now has an explicit invoice-intent target.
- The next operator action after composition is `issue_invoice`.
- Provider export, tax treatment, and legal invoice authority remain separate
  downstream boundaries; this fact does not claim any of them.
- A future issuer/exporter must consume this line explicitly before it can
  represent the adjustment in a provider document.
