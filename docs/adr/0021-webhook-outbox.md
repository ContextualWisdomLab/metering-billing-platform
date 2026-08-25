# ADR 0021: Webhook Outbox for Commercial Events

**Status:** Accepted

## Context

AIS and operators can only poll persisted journal proposals (ADR 0012).  They need an append-only push of accepted commercial facts without granting entitlement or posting statutory journals from a webhook (Fielding et al., 2022).  Tenant API credentials (ADR 0019) already persist a keyed HMAC and never the recoverable secret (Krawczyk et al., 1997; National Institute of Standards and Technology, 2020).  HTTP Message Signatures (RFC 9421) are a later structured-signature option; this slice signs the raw JSON body with HMAC-SHA256 so receivers can verify without implementing RFC 9421 (Backman et al., 2024).

AIS pull stays bootstrap.  This slice does not require AIS to subscribe, does not flip `proposal_status`, does not call AIS posting-receipt, and does not emit statutory account IDs.

## Decision

- Expose `WebhookSubscriptionService.register_subscription(tenant_reference, callback_url, event_type_codes)`.  Persist one tenant-scoped `webhook_subscription` with status `active` or `revoked`.  `callback_url` must be https.  http is allowed only for `localhost`, `127.0.0.1`, and `::1` so tests can bind a local server.
- Replay of the same tenant, callback URL, canonical event-type set, and contract version returns the same `webhook_subscription_id` as `duplicate_replay` and does not mint a second secret.
- Return the per-subscription secret once on register (`cwlwh_` plus a high-entropy token).  Persist only `webhook_secret_prefix` and `hmac-sha256:` HMAC-SHA256(pepper, secret), the same verifier pattern as ADR 0019.  List and GET never include the secret or hash.
- When a commercial fact is accepted (not replayed), append one `webhook_outbox_event` for `journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, or `credit_note.issued`.  The payload is a thin envelope `{event_type_code, occurred_at, tenant_reference, data}` wrapping the existing published contract, or a thin issued-invoice or issued-credit-note reference plus hash.  API secrets and PANs are refused.
- `WebhookDeliveryService.deliver_due_events(tenant_reference)` POSTs JSON to active same-tenant subscriptions that include the event type.  Sign with HMAC-SHA256 over the raw body.  Header is `X-CWL-Webhook-Signature: sha256=<hex>`.
- Delivery attempts are append-only `webhook_delivery_attempt` rows (`attempt_number`, `http_status`, `delivered_at` or `failure_reason_code`).  Success marks the outbox event delivered.  Later explicit runs may retry.  There is no scheduler beyond the service call and `POST /v1/webhook-deliveries`.
- HTTP on the existing WSGI app uses the tenant pin plus the ADR 0019 key rule:
  - `POST /v1/webhook-subscriptions`
  - `GET /v1/webhook-subscriptions`
  - `POST /v1/webhook-subscriptions/{id}/revoke`
  - `POST /v1/webhook-deliveries`
- Missing tenant, non-https production callbacks, unknown event types, revoked subscriptions, and secret leakage on list JSON fail closed.

## Consequences

- Operators register an https callback, then run deliveries.  AIS may keep polling.
- A process-local vault holds the minted secret for same-process delivery.  SQL never stores recoverable webhook secrets.
- A later persistent ledger can replace `MemoryUsageLedger` without changing the append-only outbox, HMAC header, or fail-closed rules.
