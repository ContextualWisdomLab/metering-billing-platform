# ADR 0034: Tenant API Credential HTTP Presentment

**Status:** Accepted

## Context

#22 already issues, lists, and revokes tenant API credentials.  `GET /v1/tenant-api-credentials` returns an unpaged metadata array.  There is no item GET.  Operators still cannot page key inventory with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

HTTP must expose the existing `tenant_api_credential` store.  It must not reconstruct a secret, change the #22 bootstrap window, or invent rotation.

## Decision

- Keep `POST /v1/tenant-api-credentials` as the #22 issue command.  Keep `POST /v1/tenant-api-credentials/{tenant_api_credential_id}/revoke` as the #22 revoke.  Refuse PAN, CVC, and provider-secret fields on those writes.
- Expose `TenantApiCredentialPresentmentService.present_tenant_api_credential(tenant_reference, tenant_api_credential_id)`.
- `GET /v1/tenant-api-credentials/{tenant_api_credential_id}` returns the tenant-scoped presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `api_credential_not_found`.
- Project stored metadata only: `tenant_api_credential_id`, `tenant_reference`, `credential_label`, `credential_prefix`, `credential_status`, `tenant_api_credential_contract_version`, `issued_at`, `revoked_at` when stored, and `next_operator_action` (`wait` while active, otherwise `issue`).
- Upgrade `GET /v1/tenant-api-credentials` to `{tenant_api_credentials, next_cursor}` ordered by `issued_at` then `tenant_api_credential_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Never return `api_credential_secret`, `credential_secret_hash`, a verifier, a keyed HMAC, or a full bearer token.
- Keep the #22 authorize rule: after one active key exists, every `/v1` call except issue requires that key.  Zero active keys keep the tenant pin.

## Consequences

- Operators issue a key, then send it on every `/v1` call; revoke when leaked.
- AIS `X-CWL-Tenant-Reference` pull stays bootstrap until a key is issued.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- Issue still mints a new append-only key.  Revoke stays idempotent.  There is no rotation command.
