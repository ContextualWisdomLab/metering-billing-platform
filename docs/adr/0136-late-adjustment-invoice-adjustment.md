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
- `IssuedInvoiceService` locks the draft, consumes linked compositions into
  signed `late_adjustment` lines, and includes them in exact totals, payload
  hashing, replay, and presentment. The composition fact itself is never
  rewritten or deleted.
- An existing tax assessment blocks issuance with
  `late_adjustment_tax_reassessment_required`; stale tax is not silently
  reused. A future tax-reassessment slice must explicitly publish the new
  assessment before issue.
- Collection, journal, provider export, and legal invoice authority remain
  separate downstream boundaries; this slice does not claim any of them.
