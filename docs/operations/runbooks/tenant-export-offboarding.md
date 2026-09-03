# Tenant export, offboarding, or retention deletion incident

**Status:** tabletop-ready; data-owner authorization and a verified export are required before release.

## Owner

The data-protection owner authorizes scope; billing operations performs the
tenant-scoped export; security reviews access and retention evidence.

## Severity and escalation

Treat a cross-tenant export, incomplete deletion, or unauthorized access as
SEV-1. Treat a delayed authorized export as SEV-2.

## Customer communication

Confirm scope, format, cutoff time, retention consequence, and handoff channel.
Never send secrets, raw provider credentials, PAN/CVC, or unrelated tenants.

## Recovery objective

Record the approved export/deletion RPO/RTO and measured duration. A successful
local file copy is not proof of a complete or authorized export.

## Evidence preservation

Preserve authorization, tenant pin, dataset manifest, row counts, content
hashes, export encryption key reference, retention decision, and deletion
receipt. Keep the export content outside incident notes.

## Detection

Verify tenant predicates on every dataset and compare the manifest with the
known ledger domains, outboxes, audit records, and object-storage references.

## Containment

Pause delivery or deletion on any scope mismatch. Use a quarantine destination
and a change record; do not delete data to make counts match.

## Diagnosis

Check identity joins, pagination boundaries, late writes, object references,
retention holds, and redaction rules. Confirm that content is not part of the
usage contract.

## Recovery

Regenerate a tenant-scoped export from immutable facts, verify checksums with
the data owner, then execute approved deletion and record each system's receipt.

## Validation receipt

Record tenant scope, manifest/hash, counts, authorization, deletion receipts,
command exit statuses, and secure evidence location. Run the repository
validator from the exact release checkout.

## Exit and RCA

Exit when the owner accepts the manifest and retention receipts and security
confirms no cross-tenant access. Record any late-arriving data and the next
retention audit.
