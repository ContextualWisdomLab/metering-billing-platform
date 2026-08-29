# ADR 0133: Present immutable late-adjustment evidence

## Status

Accepted for the next implementation slice of issue #87.

## Context

ADR 0132 records a late usage event, correction, or reversal without rewriting
the closed source period. Operators still need to inspect that durable fact
after a process restart and know what action remains. The read must preserve
the tenant boundary and must not imply that the commercial adjustment has
already changed rating, invoicing, tax, or accounting.

## Decision

Add a tenant-scoped read-only presentment contract:

- `GET /v1/late-adjustments/{late_adjustment_id}` presents the stored fact;
- `GET /v1/late-adjustments` returns `{late_adjustments, next_cursor}` with
  recorded-at/opaque-ID keyset pagination;
- the item contract publishes the signed exact amount, source and target
  period IDs, adjustment kind, source reference/hash, and recorded instant;
- `next_operator_action` is the closed value `apply_late_adjustment`.

Unknown and cross-tenant IDs are both HTTP 404. Missing tenants, malformed
UUIDs, cursors, and page limits fail closed. The read does not apply or
re-rate the adjustment, rewrite a period, create a journal or tax document,
call a provider, or emit a webhook.

## Consequences

Operators can inspect durable commercial evidence and hand it to a later
application workflow. The application/re-rating command, FX treatment,
provider settlement, FOCUS export, and statutory posting remain separate
follow-up slices.

## References

International Organization for Standardization. (2015). *Codes for the
representation of currencies* (ISO Standard No. 4217).
https://www.iso.org/standard/64758.html

PostgreSQL Global Development Group. (2026). *CREATE TRIGGER*.
https://www.postgresql.org/docs/current/sql-createtrigger.html
