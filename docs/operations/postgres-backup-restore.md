# PostgreSQL Backup and Restore

**Status:** executable repository procedure; a successful local command is not
production backup or restore evidence.

## Owner and authority

The deployment owner controls the PostgreSQL service, secret manager, backup
retention policy, restore target, and customer-impact decision. A restore that
can remove or replace objects requires the named owner’s approval and a
separate incident record.

## Preconditions

- Use an approved secret-manager or libpq environment boundary. Do not commit,
  paste, or record a DSN, password, certificate, or raw database payload.
- Confirm the source and target environment, tenant scope, retention policy,
  maintenance window, and the intended RPO/RTO measurement method.
- Install `pg_dump` and `pg_restore` from the PostgreSQL client version
  approved for the target server.
- Create a mode-`0700` evidence directory owned by the operator account.

## Create and verify an archive

```sh
umask 077
mkdir -p "$BACKUP_DIR"
BACKUP_ARCHIVE="$BACKUP_DIR/metering-billing-$(date -u +%Y%m%dT%H%M%SZ).dump"
uv run python scripts/postgres_backup.py backup \
  --dsn "$DATABASE_URL" \
  --output "$BACKUP_ARCHIVE"
uv run python scripts/postgres_backup.py verify \
  --archive "$BACKUP_ARCHIVE"
```

The helper uses PostgreSQL custom format, refuses to overwrite an existing
final archive, publishes through a same-directory temporary file, and removes
an incomplete temporary archive after a failed dump. Capture the archive SHA-256
and the two command exit statuses in the incident record without capturing
client output that could contain connection or payload data.

## Restore to an isolated target

1. Provision or select an isolated target and record its exact endpoint,
   release, schema migration state, and access boundary.
2. Verify the archive catalog again from the exact release checkout.
3. Restore without `--clean` first. Use `--clean` only for an approved,
   destructive replacement and pass `--confirm-destructive-restore` in the
   same command.

```sh
uv run python scripts/postgres_backup.py restore \
  --dsn "$RESTORE_DATABASE_URL" \
  --archive "$BACKUP_ARCHIVE"
```

The restore command uses `--exit-on-error`, `--single-transaction`, and
`--no-owner`. It does not invent missing provider, object-storage, KMS, or
identity evidence.

## Validation receipt and exit

Compare migration checksums, required table counts, append-only ledger
invariants, tenant isolation, health/readiness, and one secret-free API smoke
receipt. Record measured backup age (RPO), restore duration (RTO), archive
checksum, target identifier, exact release SHA, operator approval, and command
exit statuses. If any comparison is ambiguous, stop traffic or customer-visible
correction and escalate; never repair history with an `UPDATE` or delete.
