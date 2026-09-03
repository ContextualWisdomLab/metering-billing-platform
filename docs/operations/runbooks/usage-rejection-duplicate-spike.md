# Usage rejection or duplicate spike

**Status:** tabletop-ready; live incident evidence is required before release.

## Owner

Billing on-call owns triage. The producer owner joins when one product or SDK
version is concentrated in the spike.

## Severity and escalation

Treat sustained rejection, duplicate, or cross-tenant-denial growth as SEV-2;
escalate to the platform lead and affected product owner. Escalate to security
immediately for an attribution or tenant-isolation signal.

## Customer communication

Tell affected customers whether usage is delayed, rejected, or replayed. Do
not promise billability or losslessness until the durable outbox and receipt
population are reconciled.

## Recovery objective

Record the release profile's approved RPO/RTO and measure both in the incident
receipt. No numeric production target is claimed by this tabletop procedure.

## Evidence preservation

Preserve counts by tenant and producer, rejection reason, source-event-key
hash, SDK/schema version, trace ID, and UTC intervals. Store no event content,
prompt, response, document text, respondent response, or credential.

## Detection

Compare accepted, rejected, and duplicate-replay rates with the last measured
baseline. Check `/healthz` for process liveness and `/readyz` for dependency
readiness without logging request bodies.

## Containment

Pause only the affected producer or route through its documented kill switch;
keep already accepted usage immutable. Stop automated replay if its receipt
rate is increasing the duplicate signal.

## Diagnosis

Group by schema version, meter/unit, tenant pin, source-event-key hash, and
producer release. Verify that retry attempts reuse the same event identity and
that rejected rows have no monetary side effect.

## Recovery

Correct the producer contract or configuration, then drain its durable outbox
with bounded batches. Re-submit only the original stable event identity and
route permanent rejects to the producer's dead-letter review.

## Validation receipt

Record exact release SHA, commands and exit statuses, before/after counts,
remaining dead letters, and a secret-free checksum. Run
`python3 scripts/validate_repository.py .` from the release checkout.

## Exit and RCA

Exit when rejection and duplicate rates return to the measured envelope and
the owner accepts the evidence. Document the trigger, timeline, customer
impact, invariant preserved, corrective change, and a replay test.
