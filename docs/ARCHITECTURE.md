# Architecture

## System context

```text
Keyverse / credential issuers
        |
        v
Identity attribution
        |
CWL usage producers ---> Usage ledger ---> Metering ---> Rating
                                             |            |
                                             v            v
                                      quotas/budgets   invoice intent
                                                          |
                                                          v
                                              provider capability router
                                              /       |       |       \
                                           MoR     processor  PG    manual
                                                          |
                                                          v
                                               settlement reconciliation
                                                          |
                                                          v
                                             journal proposal export
                                                          |
                                                          v
                                          Accounting Information Platform
```

## Bounded contexts

- `identity_attribution`: principal and credential assignment.
- `usage_ingestion`: immutable events, measurements, corrections, and deduplication.
- `meter_registry`: versions, units, dimensions, aggregation, and billability.
- `commercial_rating`: price books, contracts, tiers, and rating outcomes.
- `entitlement_control`: grants, quotas, credits, and spend authorization.
- `invoice_management`: invoice intent, explainable lines, and commercial collection cases.
- `provider_gateway`: capability discovery, mapping, commands, webhooks, and later provider projections of payment intents.
- `settlement_reconciliation`: expected versus provider versus cash evidence.
- `accounting_export`: journal proposals and posting receipts.

## Authority matrix

| Fact | Authority |
| --- | --- |
| identity authentication | Keyverse or credential issuer |
| credential-to-principal attribution | Metering Billing Platform |
| usage and rating | Metering Billing Platform |
| invoice and provider payment state | Metering Billing Platform |
| tax document | MoR, tax provider, or jurisdictional issuer |
| chart of accounts and accounting policy | Accounting Information Platform |
| posted journal and trial balance | Accounting Information Platform |
| bank transaction | bank or treasury provider; accounting projection in Accounting Information Platform |

## Usage ingestion

`metering_billing.UsageIngestionService` is the write path for canonical usage events.  It validates the published schema, verifies the source-payload hash for the declared contract version, resolves tenant-scoped attribution, stores exact decimal measurements, and returns a receipt.  Optional batch bounds and usage queries use half-open ISO 8601 windows.  `POST /v1/usage-events` stays that ingest and refuses PAN and provider secrets.  `UsageEventPresentmentService` projects a stored event as a statement.  `GET /v1/usage-events/{usage_event_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/usage-events` lists `{usage_events, next_cursor}` ordered by `recorded_at` then `usage_event_id`.  Ingestion never writes a posted journal and never calls a payment provider.

## Rate-card catalog

`metering_billing.RateCardService` publishes one tenant-scoped `rate_card` and one immutable `rate_card_version` with `rate_card_line` rows.  Header identity is `(tenant_account_id, rate_card_name)`.  Version identity is `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)`.  Each line is a two-or-more-word `snake_case` `metric_code` and an exact `Decimal` `unit_amount` greater than zero in the card currency.  An identical replay returns the same `rate_card_version`.  A later distinct line set increments `version_number`.  A published version is never edited.  `POST /v1/rate-cards` publishes a version and refuses PAN and provider secrets.  `RateCardPresentmentService` projects the stored card as a statement.  `GET /v1/rate-cards/{rate_card_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/rate-cards` lists `{rate_cards, next_cursor}` ordered by `created_at` then `rate_card_id`.  Version GET routes stay the #18 catalog reads.  This path does not apply tax, discounts, or tiered prices.

## Commercial rating

`metering_billing.UsageRatingService` is the read-and-rate path for already-stored usage.  A buyer supplies a tenant, a half-open ISO 8601 window, and a persisted rate-card version.  The service resolves that same-tenant version, aggregates billable quality only, multiplies exact quantities by the stored `unit_amount` for each matching `metric_code`, and persists append-only `rating_run` and `rating_line` rows.  Identity is `(tenant_account_id, window_started_at, window_ended_at, rate_card_version_id, usage_snapshot_hash)`.  An unknown version, a cross-tenant version, or a missing metric fails closed and does not invent a price.  An identical replay returns the same `rating_run_id` and totals.  `POST /v1/rating-runs` stays that command and refuses PAN and provider secrets.  `RatingRunPresentmentService` projects a stored run as a statement.  `GET /v1/rating-runs/{rating_run_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/rating-runs` lists `{rating_runs, next_cursor}` ordered by `recorded_at` then `rating_run_id`.  Rate a window, then draft an invoice.  Rating never drafts an invoice, never calls a payment provider, and never writes a posted journal.

## Invoice draft

`metering_billing.InvoiceDraftService` copies one stored rating run into an append-only invoice-intent draft.  Identity is `(tenant_account_id, rating_run_id)` plus the rating run's usage-snapshot hash.  An identical replay returns the same `invoice_draft_id` and exact totals.  Status is `draft` only.  The draft is a commercial document, not revenue recognition and not a posted journal.

`metering_billing.InvoicePresentmentService` projects one stored draft into a tenant-scoped statement.  Tax exclusive, tax, and inclusive come from `tax_assessment` when present and otherwise zero the tax fields.  `credited_amount` is the sum of accepted credits.  `amount_due` is inclusive minus credits and never below zero.  Collection identity and outstanding appear when a case exists.  Lines map stored draft/rating quantities onto `metric_code`, `quantity`, `unit_amount`, and `line_amount`.  `GET /v1/invoice-drafts/{invoice_draft_id}` returns the statement.  `GET /v1/invoice-drafts` lists summaries with `{invoice_drafts, next_cursor}`.  The read does not write a snapshot table, post, or call AIS.  Open the draft statement, then collect or credit.

`operator_console` is the Storybook presentment surface for that JSON.  Design tokens cover color, spacing, type, and radius.  `AmountDue`, `LineTable`, `StatusChip`, and the tenant pin are tokenized modules.  Stories use taxed-plus-partial-credit, untaxed, and settled fixtures.  The console does not post, call AIS, add Stripe, or replace the Python package.  Storybook is the UI for this slice.

## Accounting export

`metering_billing.AccountingExportService` copies one stored invoice draft or one stored payment receipt into an append-only `accounting_journal_proposal`.  Draft identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)`.  Cash identity is `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)`.  Credit identity is `(tenant_account_id, credit_adjustment_id, source_payload_hash, proposal_contract_version)`.  An identical replay returns the same `proposal_id`.  Untaxed draft lines debit `accounts_receivable` and credit `usage_revenue`.  Taxed draft lines debit `accounts_receivable` for the inclusive amount, credit `usage_revenue` for the exclusive amount, and credit `tax_payable` for the tax amount.  Cash lines debit `cash_receipt` and credit `accounts_receivable`.  Untaxed credit lines debit `usage_revenue` and credit `accounts_receivable`.  Taxed credit lines debit `usage_revenue` and `tax_payable` and credit `accounts_receivable`.  Status stays inside the proposal lifecycle and is never `posted`.  AIS next pulls validated proposals.  This path does not open fiscal periods, resolve statutory account IDs, change collection outstanding, call a payment provider, or flip `proposal_status` after a pull.

## Collection case

`metering_billing.CollectionCaseService` opens an append-only commercial collection case from one stored invoice draft.  Identity is `(tenant_account_id, invoice_draft_id)`.  Outstanding equals the exact draft total.  An identical replay returns the same `collection_case_id`.  Status stays `open` or `dunning` until a receipt settles remaining outstanding to zero.  Dunning events are commercial reminders (`first_notice`, `overdue_notice`) and do not capture money or change AIS books.

## Payment intent

`metering_billing.PaymentIntentService` projects one provider-neutral `payment_intent` from a stored collection case.  Identity is `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  The hash covers the case outstanding, currency, and stored status snapshot.  Amount equals the exact case outstanding.  An identical replay returns the same `payment_intent_id`.  Status stays `projected`, `cancelled`, or `rejected` and is never `captured`, `settled`, or `posted`.  This path does not store a card PAN, call a named provider, change collection outstanding, or post a journal.  The operator next records a commercial receipt or cancels the intent.

## Payment settlement

`metering_billing.PaymentSettlementService` applies one commercial `payment_receipt` against a stored projected payment intent.  Identity is `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  The hash covers the intent amount, currency, status, and received amount.  Receipt status is `applied` only.  The linked collection-case outstanding is reduced by the same exact amount; remaining zero marks the case `settled`.  An identical replay returns the same `payment_receipt_id`.  Accept and duplicate replay idempotently call `AccountingExportService.propose_cash_journal` so AIS can pull the existing cash/AR proposal.  `POST /v1/cash-journal-proposals` remains a manual replay.  `cancel_payment_intent` flips a projected intent to `cancelled` without writing a receipt or changing outstanding.  This path does not store a card PAN, call a named provider, or post a journal.  Record the receipt; the cash journal is already validated for AIS to pull.

## HTTP accept surface

`metering_billing.http_app.create_http_app` is a stdlib WSGI adapter.  It parses JSON, requires `tenant_reference` on every write, calls the existing in-process services, and returns each `as_contract_dict` result.  Standalone serving is `python -m metering_billing.http_app` on `0.0.0.0:$PORT`.  HTTP 200 means `accepted` or `duplicate_replay` on writes, or a successful read.  HTTP 422 means `rejected` or an unreadable request.  HTTP 404 is an unknown route or an unknown/cross-tenant proposal, credit, observation, rate-card version, or invoice draft.  Money stays exact-decimal strings.  The adapter never posts a journal, never stores a card PAN, and never calls Stripe, Adyen, or Toss.

`GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}` are the AIS pull.  Tenant is required via optional `X-CWL-Tenant-Reference` or `tenant_reference` in the query or JSON body.  If both are present they must match.  Optional filters are `proposal_status`, inclusive `proposed_after`, and a bounded `cursor` / `page_limit`.  List items are the published journal-proposal contract with semantic account roles only.  Cash, AR, and credit proposals share `journal_proposal` and appear in the same list.  Query does not mutate `proposal_status`.  AIS pulls validated proposals and later returns `posting_receipt`.  That pull keeps working on the tenant pin until a key is issued for the tenant.

## Tenant API credentials

`metering_billing.WebhookSubscriptionService` registers one tenant-scoped `webhook_subscription`.  Identity is `(tenant_account_id, callback_url, event_type_set, webhook_subscription_contract_version)`.  https is required; http is allowed only for localhost tests.  The secret is returned once; SQL stores prefix plus keyed HMAC.  Accepted journal proposals, payment receipts, and credits append `webhook_outbox_event` rows.  `WebhookDeliveryService.deliver_due_events` POSTs the envelope to active matching subscriptions and signs the raw body (`X-CWL-Webhook-Signature: sha256=<hex>`).  Attempts are append-only.  `POST /v1/webhook-subscriptions` stays the #24 register and refuses PAN and provider secrets.  `POST /v1/webhook-subscriptions/{id}/revoke` stays the #24 revoke and refuses PAN and provider secrets.  `WebhookSubscriptionPresentmentService` projects stored metadata as a statement.  `GET /v1/webhook-subscriptions/{webhook_subscription_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/webhook-subscriptions` lists `{webhook_subscriptions, next_cursor}` ordered by `issued_at` then `webhook_subscription_id`.  GET never returns the secret, hash, prefix, or signed body.  `POST /v1/webhook-deliveries` uses the tenant pin plus the API-credential key rule.  `GET /v1/webhook-deliveries/{delivery_attempt_id}` presents one stored `webhook_delivery_attempt`.  `GET /v1/webhook-deliveries` lists `{webhook_deliveries, next_cursor}` ordered by `attempted_at` then `delivery_attempt_id`.  GET never resends and never returns the secret, hash, or signed body.  AIS may keep polling.  This path does not flip `proposal_status` or call AIS posting-receipt.

`metering_billing.TenantApiCredentialService` issues append-only `tenant_api_credential` rows.  Each issue mints a new secret, returns prefix plus secret once, and persists only `hmac-sha256:` HMAC-SHA256(pepper, secret).  The same tenant, optional two-or-more-word `snake_case` `credential_label`, and contract version never replay a secret.  Status is `active` or `revoked`.  `revoke_credential` is idempotent.  After one or more active keys exist, every `/v1` write and GET except credential issue requires `Authorization: Bearer <secret>` or `X-CWL-Api-Key: <secret>` whose tenant equals the pin.  Zero active keys keep the existing tenant pin (bootstrap window).  `GET /healthz` stays unauthenticated.  `POST /v1/tenant-api-credentials` issues a key and may use the tenant pin alone.  `GET /v1/tenant-api-credentials/{tenant_api_credential_id}` presents one stored credential.  `GET /v1/tenant-api-credentials` lists `{tenant_api_credentials, next_cursor}` ordered by `issued_at` then `tenant_api_credential_id`.  GET never returns the secret, hash, or verifier.  `POST /v1/tenant-api-credentials/{id}/revoke` revokes.  Unknown, revoked, and cross-tenant keys fail closed.  Issue a key, then send it on every `/v1` call; revoke when leaked.  This path does not change journal, tax, credit, or presentment shapes and does not start a web UI.

## Posting-receipt observation

`metering_billing.PostingReceiptPullService` GETs an AIS-owned `posting_receipt` from `{ais_base_url}/posting-receipts?idempotency_key=` with required `X-CWL-Tenant-Reference`.  The response is validated against the consumed AIS contract in `schemas/consumed/`.  A successful pull persists one append-only `posting_receipt_observation` identified by `(tenant_account_id, idempotency_key)` plus `source_payload_hash` / `receipt_id`.  AIS `receipt_id` is not the internal primary key.  Replay of the same tenant, key, and receipt returns the stored observation.  `posting_status_code` stays an AIS fact (`posted`, `held`, `rejected`, `reversed`) and is never mapped onto Billing `proposal_status`.  AIS 403 writes zero rows.  AIS 404 is `not_yet_accepted` and writes zero rows.  `POST /v1/posting-receipt-observations` stays that #16 pull and refuses PAN and provider secrets.  `GET /v1/posting-receipt-observations/{idempotency_key}` stays the existing #16 item read: HTTP 200 for the same tenant and HTTP 404 across tenants.  `PostingReceiptObservationPresentmentService` projects a stored observation as a statement.  `GET /v1/posting-receipt-observations` lists `{posting_receipt_observations, next_cursor}` ordered by `observed_at` then `posting_receipt_observation_id`.  Drain AIS outbox, then store the receipt observation.  This path does not flip `proposal_status`, invent a receipt shape, or start operator UI.

## Usage-event presentment

`metering_billing.UsageEventPresentmentService` projects one tenant-scoped usage statement from stored `usage_event` rows.  Identity is the stored `usage_event_id`.  Measurement quantities are the exact stored decimals.  Next operator action is `rate_window`.  `POST /v1/usage-events` remains the #5 ingest and refuses PAN and provider secrets.  `GET /v1/usage-events/{usage_event_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/usage-events` lists `{usage_events, next_cursor}` ordered by `recorded_at` then `usage_event_id`.  Ingest usage, then rate a window against a published card.  This path does not invent an ingest shape, call AIS, or start a production SPA.

## Rate-card presentment

`metering_billing.RateCardPresentmentService` projects one tenant-scoped catalog statement from stored `rate_card` and latest `rate_card_version` rows.  Identity is the stored `rate_card_id`.  Line `unit_amount` values are the exact stored prices.  Next operator action is `rate_window`.  `POST /v1/rate-cards` remains the #18 write and refuses PAN and provider secrets.  `GET /v1/rate-cards/{rate_card_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/rate-cards` lists `{rate_cards, next_cursor}` ordered by `created_at` then `rate_card_id`.  Version GET routes stay the #18 catalog reads.  Publish a rate card, then rate a window against that version.  This path does not invent a catalog, call AIS, or start a production SPA.

## Credit-adjustment presentment

`metering_billing.CreditAdjustmentPresentmentService` projects one tenant-scoped credit statement from stored `credit_adjustment` rows.  Identity is the stored `credit_adjustment_id`.  `credit_amount`, `tax_exclusive_amount`, and `tax_amount` are the exact stored amounts.  `credit_adjustment_status` stays `recorded`.  Next operator action is `wait`.  `POST /v1/credit-adjustments` remains the #17 write from `invoice_draft_id` and refuses PAN and provider secrets.  Accept still enqueues #24 `credit_adjustment.recorded` and the existing credit journal.  `GET /v1/credit-adjustments/{credit_adjustment_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/credit-adjustments` lists `{credit_adjustments, next_cursor}` ordered by `recorded_at` then `credit_adjustment_id`.  Record the credit; AIS pulls the validated journal.  This path does not invent a journal, call AIS, flip `proposal_status`, or start a production SPA.

## Payment-receipt presentment

`metering_billing.PaymentReceiptPresentmentService` projects one tenant-scoped payment-receipt statement from stored `payment_receipt` rows and the current collection case.  Identity is the stored `payment_receipt_id`.  `received_amount` is the exact stored amount.  `remaining_outstanding_amount` is the current case outstanding.  `payment_receipt_status` stays `applied`.  Next operator action is `record_receipt` or `drain_or_wait`.  `POST /v1/payment-receipts` applies against a projected `payment_intent_id` and refuses PAN and provider secrets.  Accept enqueues #24 `payment_receipt.applied` and composes the existing #13 cash journal; `POST /v1/cash-journal-proposals` remains a manual replay.  `GET /v1/payment-receipts/{payment_receipt_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/payment-receipts` lists `{payment_receipts, next_cursor}` ordered by `received_at` then `payment_receipt_id`.  Record the receipt; the cash journal is already validated for AIS to pull.  This path does not capture payment, call AIS, flip `proposal_status`, or start a production SPA.

## Payment-intent presentment

`metering_billing.PaymentIntentPresentmentService` projects one tenant-scoped payment-intent statement from stored `payment_intent` rows.  Identity is the stored `payment_intent_id`.  `payment_amount` is the exact stored amount.  `payment_intent_status` stays `projected`, `cancelled`, or `rejected`.  Next operator action is `record_receipt` or `wait`.  `POST /v1/payment-intents` projects from a stored `collection_case_id` and refuses PAN and provider secrets.  `GET /v1/payment-intents/{payment_intent_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/payment-intents` lists `{payment_intents, next_cursor}` ordered by `projected_at` then `payment_intent_id`.  Create a projected payment intent, then record the receipt.  This path does not capture payment, call AIS, flip `proposal_status`, or start a production SPA.

## Collection-case presentment

`metering_billing.CollectionCasePresentmentService` projects one tenant-scoped collection statement from stored `collection_case` and dunning rows.  Identity is the stored `collection_case_id`.  `collection_outstanding` is the exact stored outstanding.  `collection_case_status` stays `open`, `dunning`, or `settled`.  Last and next dunning notice codes use the existing `first_notice` / `overdue_notice` vocabulary.  Next operator action is `collect`, `credit`, or `wait`.  `GET /v1/collection-cases/{collection_case_id}` is HTTP 200 for the same tenant and HTTP 404 across tenants.  `GET /v1/collection-cases` lists `{collection_cases, next_cursor}` ordered by `opened_at` then `collection_case_id`.  Open the collection case, then collect or credit.  This path does not capture payment, call AIS, flip `proposal_status`, or start a production SPA.

## AIS outbox drain

`metering_billing.AisOutboxDrainService` drains AIS `GET /outbox-events?event_type_code=posting_receipt`.  The client reads `outbox_events` and `next_cursor` only.  Empty unpublished pages are success and perform zero receipt GETs.  For `posting_receipt`, Billing constructs `urn:cwl:accounting:posting_receipt:{proposal_id}` and `urn:cwl:accounting:general_journal:{proposal_id}` from stored `proposal_id` and matches those strings by equality.  It does not parse `payload_reference`.  The stored Billing `idempotency_key` is the only `GET /posting-receipts` query.  After a successful or existing observation, the drain POSTs `/outbox-events/{outbox_event_id}/publish`.  AIS 403 is not retried as another tenant.  AIS 404 does not invent a row.  `journal_reversal` and `period_close` are not drained.  `POST /v1/ais-outbox-drains` uses the tenant pin plus the API-credential key rule.  Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.

## Tax assessment

`metering_billing.TaxRateService` publishes one tenant-scoped `tax_rate_schedule` and one immutable `tax_rate_version`.  Header identity is `(tenant_account_id, tax_code)`.  Version identity is `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)`.  Closed codes are `vat`, `gst`, and `sales_tax`.  `tax_rate` is an exact `Decimal` in `[0, 1]`.  `metering_billing.TaxAssessmentService` applies a persisted version to one invoice draft.  Identity is `(tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version)`.  `tax_amount` is half-even rounded to the documented ISO 4217 minor units.  Assess after a collection case is open fails closed.  `propose_journal` then debits `accounts_receivable` inclusive, credits `usage_revenue` exclusive, and credits `tax_payable` tax.  Collection outstanding uses the inclusive amount when an assessment exists.  AIS must map `tax_payable`.  `POST /v1/tax-assessments` stays that #19 assess command and refuses PAN and provider secrets.  `GET /v1/tax-assessments/{tax_assessment_id}` stays the existing #19 item read: HTTP 200 for the same tenant and HTTP 404 across tenants.  `TaxAssessmentPresentmentService` projects a stored assessment as a statement.  `GET /v1/tax-assessments` lists `{tax_assessments, next_cursor}` ordered by `assessed_at` then `tax_assessment_id`.  Publish a tax rate, assess the draft, then propose the journal and let AIS pull.  This path does not call an OSS engine, store exemptions, invent a journal, or start operator UI.

## Credit adjustment

`metering_billing.CreditAdjustmentService` records one commercial `credit_adjustment` against a stored invoice draft.  Identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)`.  `credit_amount` is an exact `Decimal` greater than zero and cannot exceed remaining adjustable consideration.  Currency is copied from the draft.  Closed reasons are `rating_correction`, `goodwill`, and `billing_error`.  If a collection case exists, outstanding is reduced by the same inclusive amount; remaining zero marks the case `settled`.  When a tax assessment exists, `tax_exclusive_amount` and `tax_amount` are the proportional split `round_half_even(credit_amount * tax_amount / tax_inclusive_amount)`.  A taxed journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`.  Untaxed credits stay two-line.  An identical replay returns the same `credit_adjustment_id` and `proposal_id`.  `POST /v1/credit-adjustments` records the credit.  `GET /v1/credit-adjustments/{credit_adjustment_id}` is tenant-scoped and does not call AIS.  Operators record the credit; AIS pulls the validated three-line unwind.  This path does not post, open a fiscal period, emit statutory account IDs, call AIS, or start refund-to-card or chargeback.

## Failure policy

- Duplicate input returns the existing receipt.
- A source-event key replay with a different payload hash or contract version is a conflict.
- Attribution URNs that leave the event tenant are rejected.
- Invalid quality or meter configuration fails closed.
- Provider timeouts retain internal facts and retry idempotently.
- Existing provider objects never fail over automatically to a different provider.
- Accounting rejection does not rewrite billing facts; it creates a reconciliation exception.
