# ADR 0019: Tenant API Credentials for HTTP

**Status:** Accepted

## Context

The HTTP accept surface (ADR 0011) pins a tenant with `X-CWL-Tenant-Reference` or `tenant_reference` and otherwise accepts any caller who knows that reference.  That is not shippable under SOC 2 CC6 logical access control (American Institute of Certified Public Accountants, 2017).  NIST SP 800-63B requires verifier secrets to be stored as a salted or keyed hash, never in recoverable form (National Institute of Standards and Technology, 2020).  OWASP API authentication treats leaked keys as revocable bearer credentials (OWASP, 2023).

AIS already pulls journal proposals with `X-CWL-Tenant-Reference`.  This slice must not force AIS to send a key until an operator issues one for that tenant.  Journal, tax, credit, and presentment shapes stay unchanged.  This slice does not start a web UI.

## Decision

- Expose `TenantApiCredentialService.issue_credential(tenant_reference)`.  Each issue mints a new secret (`cwlak_` plus a high-entropy token), returns prefix plus secret once, and persists only `hmac-sha256:` HMAC-SHA256(pepper, secret).  The same tenant, optional `credential_label`, and contract version never replay a secret.
- Optional `credential_label` is two-or-more-word `snake_case`.  Omitted or empty labels become `operator_key`.
- Persist append-only `tenant_api_credential` rows.  Status is `active` or `revoked`.  `revoke_credential(tenant, credential_id)` is idempotent.  Revoked and unknown keys fail closed as `api_credential_invalid`.
- Authorize existing WSGI `/v1` writes and GETs with `Authorization: Bearer <secret>` or `X-CWL-Api-Key: <secret>`.  After one or more active keys exist for the tenant, every `/v1` call except credential issue requires a matching active key whose tenant equals the pin (mismatch is 422).  Zero active keys keep the existing tenant pin (bootstrap window).  `GET /healthz` stays unauthenticated.
- `POST /v1/tenant-api-credentials` issues a key and may use the tenant pin alone.  `GET /v1/tenant-api-credentials` lists metadata only (`tenant_api_credential_id`, `credential_label`, `credential_prefix`, `credential_status`, `issued_at`).  `POST /v1/tenant-api-credentials/{id}/revoke` revokes.  The secret never appears on GET, list, revoke, logs, or AIS contracts.
- Do not change journal, tax, credit, or presentment shapes.  Do not require AIS to send a key until a key is issued for that tenant.

## Consequences

- Operators issue a key, then send it on every `/v1` call; revoke when leaked.
- AIS `X-CWL-Tenant-Reference` pull keeps working until a key is issued for that tenant.
- Revoking the last active key reopens the bootstrap window.
- A later persistent ledger can replace `MemoryUsageLedger` without changing the issue-once or fail-closed rules.
