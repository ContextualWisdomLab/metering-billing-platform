# ADR 0131: PostgreSQL backup and restore rehearsal boundary

**Status:** Accepted

## Context

Issue #84 requires restart, concurrency, failover, backup, restore, and
hot-partition evidence for the durable PostgreSQL path. The repository had a
migration runner and a real PostgreSQL ledger but no repeatable backup/restore
rehearsal boundary. A runbook alone would not prove that a restored database
contains the same commercial records.

## Decision

- Use PostgreSQL's standard `pg_dump --format=custom` and `pg_restore` tools.
- Open one read-only repeatable-read transaction, export its PostgreSQL
  snapshot, and capture counts and `pg_dump` from that same snapshot. Store
  those counts and the dump SHA-256 in a closed, secret-free manifest.
- Publish the dump by a non-replacing hard link and remove only that owned link
  if manifest publication fails; never replace a pre-existing artifact.
- Restore only when the operator explicitly marks the target disposable. The
  script uses `--clean` and `--if-exists` only on that acknowledged target.
- Re-query the restored target through `psql` and fail closed on any digest,
  manifest, command, or row-count mismatch.
- Accept credentials only through external libpq configuration; inline
  passwords in DSNs are rejected and no command output is copied into errors.

## Consequences

The rehearsal is executable and safe to repeat with a disposable target. It
proves a small, stable domain-count comparison and artifact-integrity check
without mixing concurrent committed writes between the counts and dump, but it
does not claim PITR, disaster recovery, managed backups, encryption, tenant
offboarding, or an RPO/RTO. Those require separate operational evidence and
remain open under #84.

## References

- PostgreSQL Global Development Group. (2026). *PostgreSQL 18
  documentation: `pg_dump`, `pg_restore`, and `psql`*.
- Fielding, R., Nottingham, M., & Reschke, J. (2022). *HTTP Semantics*
  (RFC 9110).
