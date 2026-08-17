# ADR 0035: Webhook Subscription HTTP Presentment

**Status:** Accepted

## Context

#24 already registers, lists, and revokes webhook subscriptions.  `GET /v1/webhook-subscriptions` returns an unpaged metadata array.  There is no item GET.  Operators still cannot page callback inventory with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

HTTP must expose the existing `webhook_subscription` store.  It must not reconstruct a secret, change the #24 callback URL SSRF policy, HMAC signature header, secret one-time-return, or known event types, or invent rotation or retry.

## Decision

- Keep `POST /v1/webhook-subscriptions` as the #24 register command.  Keep `POST /v1/webhook-subscriptions/{webhook_subscription_id}/revoke` as the #24 revoke.  Refuse PAN, CVC, and provider-secret fields on those writes.
- Expose `WebhookSubscriptionPresentmentService.present_webhook_subscription(tenant_reference, webhook_subscription_id)`.
- `GET /v1/webhook-subscriptions/{webhook_subscription_id}` returns the tenant-scoped presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `webhook_subscription_not_found`.
- Project stored metadata only: `webhook_subscription_id`, `tenant_reference`, `callback_url`, `event_type_codes`, `subscription_status`, `webhook_subscription_contract_version`, `issued_at`, `revoked_at` when stored, and `next_operator_action` (`run_deliveries` while active, otherwise `register`).
- Upgrade `GET /v1/webhook-subscriptions` to `{webhook_subscriptions, next_cursor}` ordered by `issued_at` then `webhook_subscription_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Never return `webhook_secret`, `webhook_secret_hash`, `webhook_secret_prefix`, a signature key, `payload_json`, or a signed body.
- Keep the #22 authorize rule and the #24 SSRF, HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, secret one-time-return, and known event-type contracts.

## Consequences

- Operators register an https callback, then run deliveries; AIS may keep polling.
- Register still mints a secret once.  Revoke stays idempotent.  There is no rotation or retry command.
- Storybook can consume this JSON.  This slice does not add a production SPA.
