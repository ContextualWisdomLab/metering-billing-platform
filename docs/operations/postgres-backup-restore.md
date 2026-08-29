# PostgreSQL backup and restore rehearsal

This runbook creates a PostgreSQL custom-format backup and verifies a restore
into a disposable database. It is evidence of one rehearsal, not proof of
point-in-time recovery, disaster-recovery RPO/RTO, encryption, or a managed
provider SLA.

## Prerequisites

- PostgreSQL client tools `pg_dump`, `pg_restore`, and `psql` are installed.
- `METERING_BILLING_POSTGRES_DSN` identifies the source database.
- `METERING_BILLING_RESTORE_DSN` identifies an already-created disposable
  target database.
- DSNs contain no inline password. Use libpq's external secret configuration
  (`PGSERVICE`, `PGPASSFILE`, workload identity, or an equivalent secret
  provider) and keep that configuration outside the repository and logs.
- The source schema is migrated and the target is disposable. The restore
  command uses `--clean` only after the explicit disposable-target flag.
- Counts and `pg_dump` share one exported PostgreSQL repeatable-read snapshot,
  so concurrent committed writes are not mixed into the evidence pair.

## Create evidence

```bash
mkdir -p ./var/backup-evidence
METERING_BILLING_POSTGRES_DSN='dbname=metering_billing' \
  uv run --locked --group dev python scripts/backup_restore_postgres.py \
  --mode create \
  --backup-path ./var/backup-evidence/metering_billing.dump
```

The command writes the dump and adjacent
`metering_billing.dump.manifest.json`. The manifest contains only the dump
SHA-256 and counts for migration history, tenants, and usage events; it never
contains a DSN or secret.

## Restore and verify

Create or reset the disposable target using the operator's normal PostgreSQL
administration path, then run:

```bash
METERING_BILLING_RESTORE_DSN='dbname=metering_billing_restore' \
  uv run --locked --group dev python scripts/backup_restore_postgres.py \
  --mode restore-verify \
  --backup-path ./var/backup-evidence/metering_billing.dump \
  --target-is-disposable
```

The command refuses a missing disposable-target acknowledgement, a changed
dump, a malformed manifest, a non-zero PostgreSQL command, or row-count drift.
If publication or manifest writing fails, the invocation removes only its own
backup hard link so a retry cannot inherit an orphaned evidence artifact.
Record the command output, exact repository commit, database version, machine
context, elapsed time, and operator in the incident/finance evidence store.

## Recovery limits

The current script is a bounded logical backup rehearsal. It does not perform
WAL archiving, point-in-time recovery, cross-region replication, tenant export,
encryption-key rotation, or automatic database creation. Those remain open
acceptance criteria for issue #84 and must not be represented as GA evidence.
