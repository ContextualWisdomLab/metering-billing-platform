# Database failover, corruption, or restore incident

**Status:** tabletop-ready; a timed isolated restore rehearsal is required before release.

## Owner

Database on-call owns the database; billing on-call owns traffic readiness and
commercial evidence; the change owner approves cutover.

## Severity and escalation

Use SEV-1 for corruption, unavailable primary storage, or suspected data loss.
Use SEV-2 for a controlled failover with no integrity signal. Page security if
access boundaries or backups are exposed.

## Customer communication

Report read/write availability and whether accepted usage is queued. Do not
claim recovery until migration state and immutable facts are verified.

## Recovery objective

Record profile-specific RPO/RTO before the rehearsal and replace it with the
timed result. A Compose startup or migration pass alone is not restore proof.

## Evidence preservation

Preserve backup ID, database instance, migration checksums, last known good
timestamp, row-count/hash checks, and cutover times. Never place DSNs or backup
contents in the ticket.

## Detection

Separate `/healthz` process liveness from `/readyz` database/migration
readiness. Inspect migration history, connection errors, storage alerts, and
tenant-isolation probes without dumping tables.

## Containment

Stop writes only under the approved change record and preserve the original
volume. Use an isolated restore target; never test recovery by deleting or
overwriting the production volume.

## Diagnosis

Classify connection failure, failed migration, checksum drift, storage loss,
lock exhaustion, corruption, or application defect. Compare immutable counts
and checksums at the last known good point.

## Recovery

Restore to the approved target, apply checked-in migrations with
`scripts/migrate_postgres.py`, verify readiness and tenant boundaries, then
cut over in the documented order. Reconcile accepted outbox events after cutover.

## Validation receipt

Record backup/restore IDs, measured RPO/RTO, migration output, readiness output,
integrity checks, command exit statuses, and evidence checksums. Run the
repository validator from the release checkout.

## Exit and RCA

Exit only after the change owner accepts the timed rehearsal or recovery,
backups are current, and queued work has a clear replay decision. Record the
root cause and the next restore exercise.
