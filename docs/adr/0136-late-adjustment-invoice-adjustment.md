# ADR 0136: Compose a Rated Late Adjustment into an Invoice Draft

## Status

Accepted for the #87 late-period adjustment slice.

## Decision

Record a rated late adjustment as a separate immutable
`late_adjustment_invoice_adjustment` fact linked to one same-tenant invoice
draft. The fact copies the signed amount, currency, target period, application,
and rating identities, records the single billing account selected from the
draft lines, and adds operator and authorization evidence.

The command is exposed at
`POST /v1/late-adjustments/{late_adjustment_id}/invoice-adjustments` and
requires `invoice_draft_id`, `recorded_by`, and
`authorization_reference`. Its identity is the tenant and rated adjustment;
replays return the stored composition. A currency mismatch, cross-tenant draft,
already-issued draft, downstream collection/journal/tax/credit fact, ambiguous
billing account, or unrepresentable amount is rejected. Existing drafts and
issued invoice snapshots are never overwritten.
The composition audit instant is required to be timezone-aware and not
future-dated at the service, repository, and PostgreSQL boundaries.

## Consequences

- The signed late-adjustment delta now has an explicit invoice-intent target.
- A composition is single-payer only: no draft lines or multiple billing
  accounts fail closed, and the selected tenant-scoped account is copied to
  the issued line. This slice does not infer a payer from the tenant.
- The next operator action after composition is `issue_invoice`.
- `IssuedInvoiceService` locks the draft, consumes linked compositions into
  signed `late_adjustment` lines, and includes them in exact totals, payload
  hashing, replay, and presentment. The composition fact itself is never
  rewritten or deleted.
- An existing tax assessment blocks issuance with
  `late_adjustment_tax_reassessment_required`; stale tax is not silently
  reused. A future tax-reassessment slice must explicitly publish the new
  assessment before issue.
- Composition is rejected after collection, journal, tax, or credit facts have
  captured the draft. A zero resulting issue is rejected because the existing
  collection workflow has no zero-value action.
- Once composition exists, collection, tax, journal, and credit writes reject
  with `invoice_draft_has_late_adjustment` under the same invoice-draft lock;
  migration `0057` enforces this ordering for direct PostgreSQL inserts.
  Migration `0058` also locks the draft before rejecting a direct composition
  after an existing downstream fact, while allowing an existing composition
  identity to replay. Issuance preserves exact representable totals and rejects
  more than 10,000 projected lines.
- Composition writes require contract version 2. Migration `0059` fails closed
  for legacy composition rows without billing-account evidence, upgrades only
  compatible version metadata, and then enforces the version in PostgreSQL.
  Historical v1 issued-invoice snapshots remain readable because presentment
  upgrades only the response envelope.
- Migration `0060` rejects amounts that cannot be represented by
  `numeric(38,12)`, validates direct issued-line draft/amount/payer equality,
  and permits post-issue collection only from the frozen inclusive total.
- Migration `0061` uses deferred PostgreSQL checks to require every linked
  composition to appear once in issued lines and in the frozen exclusive total;
  the check takes the shared invoice-draft lock before comparing facts.
- Migration `0062` enforces issued-invoice snapshot and line immutability at the
  database boundary and removes the issued-line `line_type` default.
- Issued-invoice and presentment line envelopes are contract version 2. Audit
  actor, authorization, and timestamp remain first-write evidence and are not
  part of the replay identity. Historical stored v1 invoices remain readable
  because presentment upgrades only the response envelope.
- Historical v1 invoice replay keeps the existing `invoice.issued` outbox fact
  by source identity; upgrading the current response envelope does not emit a
  second webhook fact.
- Collection, journal, provider export, and legal invoice authority remain
  separate downstream boundaries; this slice does not claim any of them.
