# ADR 0037: Webhook Outbox Event HTTP Presentment

**Status:** Accepted

## Context

#24 already enqueues append-only `webhook_outbox_event` rows when a journal proposal is validated, a payment receipt is applied, or a credit is recorded.  #36 webhook-delivery presentment exposes stored `webhook_delivery_attempt` rows that those events feed.  Operators still cannot GET one stored commercial outbox event or page the tenant backlog.  This store is the Billing commercial webhook outbox, not the AIS posting-receipt outbox.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

HTTP must expose the existing `webhook_outbox_event` store.  It must not publish, send, retry, mark delivered, or leak `payload_json` or webhook secrets.

## Decision

- Keep commercial-fact enqueue and `POST /v1/webhook-deliveries` as the #24 write path.  Refuse PAN, CVC, and provider-secret fields on that write.  Do not add a publish or retry command.
- Expose `WebhookOutboxEventPresentmentService.present_webhook_outbox_event(tenant_reference, outbox_event_id)`.
- `GET /v1/webhook-outbox-events/{outbox_event_id}` returns the tenant-scoped presentment.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 `webhook_outbox_event_not_found`.
- Project stored metadata only: `outbox_event_id`, `tenant_reference`, `event_type_code`, `source_id`, `payload_hash`, `occurred_at`, `enqueued_at`, `delivery_status`, `attempted_delivery_count`, and `next_operator_action` (`run_deliveries` while pending, otherwise `wait`).
- Expose `GET /v1/webhook-outbox-events` as `{webhook_outbox_events, next_cursor}` ordered by `enqueued_at` then `outbox_event_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Never return `payload_json`, raw body, webhook secret, hash, prefix, signature, callback auth, or a PII blob.
- Leave #24 known event types, HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, SSRF policy, secret one-time return, and delivery behavior unchanged.  Do not touch AIS outbox drain.

## Consequences

- Operators inspect the commercial webhook backlog, then run deliveries.
- Enqueue and delivery stay the #24 rules.  GET never publishes or sends.
- Storybook can consume this JSON.  This slice does not add a production SPA.
