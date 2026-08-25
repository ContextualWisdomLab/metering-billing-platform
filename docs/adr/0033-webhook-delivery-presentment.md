# ADR 0033: Webhook Delivery HTTP Presentment

**Status:** Accepted

## Context

#24 already registers webhook subscriptions and runs deliveries through `WebhookDeliveryService.deliver_due_events` and `POST /v1/webhook-deliveries`.  Attempts are append-only `webhook_delivery_attempt` rows identified by `delivery_attempt_id`.  Operators still cannot audit one attempt or page stored attempts with a keyset cursor.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  HTTP must expose the existing #24 store.  It must not resend, invent a delivery status, change HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, or leak webhook secrets or signed raw bodies.

## Decision

- Keep `POST /v1/webhook-deliveries` as the #24 deliver-due-events command.  Refuse PAN, CVC, and provider-secret fields on the write.  The response stays the existing run-summary contract.
- Expose `WebhookDeliveryPresentmentService.present_webhook_delivery(tenant_reference, delivery_attempt_id)`.
- `GET /v1/webhook-deliveries/{delivery_attempt_id}` returns the tenant-scoped presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.
- Project stored fields only: `delivery_attempt_id`, `tenant_reference`, `webhook_subscription_id`, `outbox_event_id`, `event_type_code`, `source_id`, `attempt_number`, `http_status` when stored, `failure_reason_code` when stored, `attempted_at`, `delivered_at` when stored, and `next_operator_action` (`wait` after stored success, otherwise `run_deliveries`).
- Expose `GET /v1/webhook-deliveries` as `{webhook_deliveries, next_cursor}` ordered by `attempted_at` then `delivery_attempt_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Never return `webhook_secret`, `webhook_secret_hash`, `payload_json`, or a signed raw body.  Do not invent `delivery_status`.
- Do not add a presentment table, new event type, callback SSRF policy, secret format, AIS call, or journal.

## Consequences

- Operators register an https callback, then run deliveries; AIS may keep polling.
- Storybook can consume this JSON.  This slice does not add a production SPA.
- HMAC `X-CWL-Webhook-Signature: sha256=<hex>` and the #24 write contract stay unchanged.
