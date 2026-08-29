# Credential or webhook-secret leak incident

**Status:** tabletop-ready; security incident evidence is required before release.

## Owner

Security on-call owns containment. The tenant owner and billing on-call handle
credential rotation and delivery recovery.

## Severity and escalation

Treat any exposed active credential or webhook secret as SEV-1. Escalate to the
security lead, affected tenant, and provider owner through the approved secure
channel.

## Customer communication

Notify only affected parties with the minimum actionable detail. Never repeat
the secret, token, signature, PAN/CVC, or raw request in the notification.

## Recovery objective

Record the security profile's RPO/RTO and measured time to revoke, rotate, and
verify. Do not use the presence of a revocation endpoint as timing evidence.

## Evidence preservation

Preserve secret type, prefix or keyed fingerprint, exposure window, access
logs, affected tenant, and rotation IDs. Store hashes and redacted metadata,
never recoverable secret material.

## Detection

Search access-control and delivery telemetry for the credential fingerprint,
unexpected tenant/use-purpose combinations, and failed signature verification.
Avoid content-bearing log searches.

## Containment

Revoke the affected tenant credential or webhook subscription, block the
exposure path, and rotate through the approved secret manager. Do not edit
historical payloads to remove evidence.

## Diagnosis

Determine source, first exposure, last use, tenant scope, and whether the key
was accepted for an unauthorized purpose. Check that only keyed hashes and
prefixes are persisted.

## Recovery

Issue a replacement with least privilege, update the producer/provider through
the secure channel, replay only verified stable events, and monitor for reuse
of the revoked fingerprint.

## Validation receipt

Record revoke/rotate IDs, exposure interval, access review, command exit
statuses, and secret-free checksums. Run the repository validator from the
exact release checkout.

## Exit and RCA

Exit after revocation and replacement are independently verified and the tenant
confirms service. Record notification, evidence retention, control weakness,
and a secret-scanning regression.
