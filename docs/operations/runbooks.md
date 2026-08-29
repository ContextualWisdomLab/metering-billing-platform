# Operational Runbooks

**Status:** tabletop-ready support procedures; not evidence of a live production rehearsal.

These procedures keep the commercial ledger, customer communication, and incident
evidence separate. An operator must use the exact deployed release SHA and a
dedicated incident record. Never paste prompts, responses, document text,
respondent responses, credentials, webhook secrets, PAN/CVC, or raw provider
payloads into an incident system.

## Runbook index

| Scenario | Procedure |
| --- | --- |
| Usage rejection or duplicate spike | [usage-rejection-duplicate-spike](runbooks/usage-rejection-duplicate-spike.md) |
| Rating or price mismatch | [rating-price-mismatch](runbooks/rating-price-mismatch.md) |
| Budget-control failure | [budget-control-failure](runbooks/budget-control-failure.md) |
| Provider outage, throttling, or webhook backlog | [provider-outage-throttling](runbooks/provider-outage-throttling.md) |
| Reconciliation mismatch | [reconciliation-mismatch](runbooks/reconciliation-mismatch.md) |
| Database failover, corruption, or restore | [database-failover-restore](runbooks/database-failover-restore.md) |
| Credential or webhook-secret leak | [credential-or-webhook-secret-leak](runbooks/credential-or-webhook-secret-leak.md) |
| Incorrect invoice, refund, dispute, settlement, or closed-period adjustment | [commercial-correction](runbooks/commercial-correction.md) |
| Tenant export, offboarding, or retention deletion | [tenant-export-offboarding](runbooks/tenant-export-offboarding.md) |
| Vulnerability or dependency emergency | [vulnerability-dependency-emergency](runbooks/vulnerability-dependency-emergency.md) |

## Execution contract

1. Record incident ID, operator role, exact release SHA, environment, tenant
   scope, start time, and the current customer impact before changing state.
2. Preserve append-only facts and hashes first. Do not repair history with an
   UPDATE, delete a receipt, or replay a provider payload under a new identity.
3. Use `/healthz` for process liveness and `/readyz` for traffic readiness. A
   readiness failure is not permission to discard accepted usage.
4. Run the repository contract check from the exact release checkout:
   `python3 scripts/validate_repository.py .`.
5. Attach a secret-free validation receipt containing command, exit status,
   timestamps, affected identifiers, and evidence checksums. Record measured
   RPO/RTO rather than copying an aspirational target.
6. Obtain the named owner’s approval before a customer-visible correction,
   retention deletion, restore cutover, or security containment that changes
   access.

## Current evidence boundary

The repository has a real Compose deployment, PostgreSQL migration runner,
health endpoints, durable outbox paths, and local contract validation. Live
provider, object-storage, KMS, backup/restore, and production RPO/RTO evidence
must be attached by the deployed service owner; these documents do not invent
that evidence.

## Deployment-owned references

Before executing a procedure, the deployment owner adds the approved links for
provider status and retry controls, the secret/KMS authority, object storage,
backup and restore service, tenant export/deletion workflow, and budget
reservation controller to the incident record. Repository-owned references are
the [Compose topology](../../compose/docker-compose.yml), [migration runner](../../scripts/migrate_postgres.py),
[PostgreSQL backup helper](../../scripts/postgres_backup.py), [backup and restore procedure](postgres-backup-restore.md),
[load baseline](load-test-baseline.md),
[security boundary](../SECURITY.md),
and [repository validator](../../scripts/validate_repository.py). An absent
external link is an evidence gap and must not be replaced with an invented
production claim.
