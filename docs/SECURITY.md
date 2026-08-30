# Security

This repository is the CWL commercial usage and billing authority.  It is not the statutory accounting authority.  HTTP access control for tenant-scoped `/v1` calls is a Billing fact.  It does not grant entitlement, post a journal, or store a card PAN.

## Customer copy

Issue a key, then send it on every `/v1` call; revoke when leaked.

Register an https callback, then run deliveries; AIS may keep polling.

Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.

```bash
# Bootstrap: tenant pin only, until a key exists for that tenant.
# POST /v1/tenant-api-credentials
# {"tenant_reference":"urn:cwl:tenant_001","credential_label":"operator_key"}

# After issue, send the secret on every /v1 call:
# Authorization: Bearer <secret>
# or
# X-CWL-Api-Key: <secret>
# plus X-CWL-Tenant-Reference or tenant_reference. They must match the key's tenant.

# GET /v1/tenant-api-credentials lists id, label, prefix, status, and issued_at.
# It never returns the secret or the stored hash.
# POST /v1/tenant-api-credentials/{id}/revoke
```

`GET /healthz` stays unauthenticated.

## Bootstrap window

A tenant with zero active credentials keeps the existing tenant pin.  AIS can keep pulling journal proposals with `X-CWL-Tenant-Reference` until an operator issues a key for that tenant.  After one or more active keys exist, missing, unknown, and revoked secrets fail closed.  Revoking the last active key reopens the bootstrap window.  `POST /v1/tenant-api-credentials` may still use the tenant pin alone so an operator can mint a replacement.

## Stored secrets

NIST SP 800-63B requires verifier secrets to be stored as a salted or keyed hash, never in recoverable form (National Institute of Standards and Technology, 2020).  This repository persists `hmac-sha256:` HMAC-SHA256(pepper, secret) on `tenant_api_credential` and `webhook_subscription`.  The plaintext secret is returned once on issue or register and is never logged, listed, placed on AIS contracts, or returned from webhook-delivery, webhook-subscription, or tenant-api-credential presentment.

OWASP API authentication treats leaked keys as revocable bearer credentials (OWASP, 2023).  `revoke_credential` is idempotent.  Unknown and revoked keys are indistinguishable (`api_credential_invalid`).

SOC 2 CC6 requires logical access control before a shippable HTTP surface (American Institute of Certified Public Accountants, 2017).  A presented key whose tenant does not equal `X-CWL-Tenant-Reference` / `tenant_reference` is `request_invalid`.  A missing tenant when a key is required is `tenant_not_found`.  A missing key after bootstrap closes is `api_credential_missing`.

## Fail closed

- Unknown or revoked key: `api_credential_invalid`
- Key for tenant A used with tenant B's pin: `request_invalid`
- Missing tenant when a key is required: `tenant_not_found`
- Missing key after an active key exists: `api_credential_missing`
- Header mismatch between `Authorization` and `X-CWL-Api-Key`: `request_invalid`
- Non-Bearer `Authorization` or an empty Bearer secret: `api_credential_invalid`
- Non-https production webhook callback: `webhook_callback_url_insecure`
- Unknown webhook event type: `webhook_event_type_unknown`
- Unknown or cross-tenant webhook subscription: `webhook_subscription_not_found`
- Unknown or cross-tenant late adjustment: `late_adjustment_not_found`
- Missing or blank late-adjustment actor reference: `actor_reference_invalid`
- Missing or blank late-adjustment authorization reference: `authorization_reference_invalid`
- Rating an unapplied late adjustment: `late_adjustment_application_not_found`
- Applying after the target period closes: `late_adjustment_target_period_not_open`
- Rating after the target period closes: `late_adjustment_target_period_not_open`
- Unknown or cross-tenant late-adjustment rating source: `late_adjustment_not_found`
- Unknown or cross-tenant invoice draft for a rated adjustment: `invoice_draft_not_found`
- Invoice-draft currency different from the rated adjustment: `currency_mismatch`
- Rated adjustment already attached to another draft: `late_adjustment_invoice_adjustment_identity_conflict`
- Rated adjustment targeting an issued invoice draft: `invoice_already_issued`
- Composition after collection, journal, tax, or credit capture:
  `invoice_draft_has_downstream_records`
- New collection, journal, tax-assessment, or credit writes before issuance after
  composition: `invoice_draft_has_late_adjustment`; the draft lock and
  PostgreSQL migrations `0057`/`0058` prevent stale downstream capture.
  Migration `0059` fails closed on legacy compositions without payer evidence,
  upgrades compatible version metadata, and enforces composition contract
  version 2. After issuance, collection is allowed only against the frozen
  issued currency and inclusive total; other downstream writes remain blocked.
- Direct PostgreSQL issued lines must match their composition's draft, signed
  amount, absolute unit price, and billing-account reference. Composition
  amounts that would round in `numeric(38,12)` are rejected by migration `0060`.
  Migration `0061` rejects direct issued headers that omit linked composition
  lines or freeze a total that excludes a signed adjustment, after taking the
  shared invoice-draft lock.
- Migration `0062` rejects direct UPDATE/DELETE of issued-invoice snapshots and
  lines and removes the issued-line `line_type` default so a direct insert cannot
  silently become a usage line.
- Composition or direct persistence with a contract version other than 2 is
  rejected; stored v1 issued-invoice snapshots are upgraded only at the
  presentment and issuance-replay envelopes and are not rewritten.
- Draft with no single tenant-scoped payer:
  `invoice_draft_billing_account_not_found` or
  `invoice_draft_billing_account_ambiguous`
- Adjustment that would round in issued-invoice storage:
  `adjustment_amount_not_representable`
- Late-adjustment composition on a draft with an existing tax assessment:
  `late_adjustment_tax_reassessment_required`; stale tax is never reused.
- Rejected late-adjustment application, rating, and invoice-adjustment command
  writes, including missing source records, return HTTP 422; only reads use
  HTTP 404 to hide unknown or cross-tenant resources.

Do not store card data, PAT plaintext, prompt text, response text, provider secrets, or webhook-subscription plaintext.  Do not start a web UI in this slice.
