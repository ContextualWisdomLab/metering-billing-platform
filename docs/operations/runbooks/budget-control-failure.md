# Budget-control failure incident

**Status:** tabletop-ready; live authorization evidence is required before release.

## Owner

The control-plane on-call owns containment; the tenant and product owners
approve resumed work or customer communication.

## Severity and escalation

Treat fail-open authorization, reservation leakage, or cross-tenant denial as
SEV-1 and page security. Treat isolated fail-closed denials as SEV-2.

## Customer communication

Explain whether work is blocked, queued, or continuing under a safe hold. Do
not claim that a published budget is an entitlement until reserve/commit/release
evidence exists.

## Recovery objective

Record the approved RPO/RTO and measured time to restore safe authorization.
Until measured, the service remains in the documented non-GA evidence state.

## Evidence preservation

Preserve tenant, billing account, budget ID, operation ID, exact amount,
currency, decision code, and idempotency key. Redact credentials and operation
content.

## Detection

Compare authorization accepts, denials, expired reservations, commits, and
releases by tenant. Check for negative remaining, duplicate decisions, or a
decision without a durable receipt.

## Containment

Fail closed for uncertain decisions and stop only the affected expensive
operation class. Do not manually increment a budget or delete a reservation.

## Diagnosis

Check effective dates, currency, tenant/account ownership, concurrent writes,
retry identity, and the rating snapshot used by the control. Separate a
published budget from an enforceable reservation.

## Recovery

Repair the control path, replay only the stable decision identity, and reconcile
reserved versus committed versus released amounts before reopening work.

## Validation receipt

Record invariant checks, affected IDs, before/after exact decimals, command
exit statuses, and owner approval. Run the repository validator from the exact
release checkout.

## Exit and RCA

Exit when uncertain work is safe, all reservations have one terminal outcome,
and the owner accepts the receipt. Add concurrency and retry coverage for the
failure mode.
