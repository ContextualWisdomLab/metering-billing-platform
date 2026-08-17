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

`metering_billing.UsageIngestionService` is the write path for canonical usage events.  It validates the published schema, verifies the source-payload hash for the declared contract version, resolves tenant-scoped attribution, stores exact decimal measurements, and returns a receipt.  Optional batch bounds and usage queries use half-open ISO 8601 windows.  Ingestion never writes a posted journal and never calls a payment provider.

## Rate-card catalog

`metering_billing.RateCardService` publishes one tenant-scoped `rate_card` and one immutable `rate_card_version` with `rate_card_line` rows.  Header identity is `(tenant_account_id, rate_card_name)`.  Version identity is `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)`.  Each line is a two-or-more-word `snake_case` `metric_code` and an exact `Decimal` `unit_amount` greater than zero in the card currency.  An identical replay returns the same `rate_card_version`.  A later distinct line set increments `version_number`.  A published version is never edited.  `POST /v1/rate-cards` publishes a version.  Tenant-scoped GET list, card, versions, and version routes fail closed across tenants.  This path does not apply tax, discounts, or tiered prices.

## Commercial rating

`metering_billing.UsageRatingService` is the read-and-rate path for already-stored usage.  A buyer supplies a tenant, a half-open ISO 8601 window, and a persisted rate-card version.  The service resolves that same-tenant version, aggregates billable quality only, multiplies exact quantities by the stored `unit_amount` for each matching `metric_code`, and persists append-only `rating_run` and `rating_line` rows.  Identity is `(tenant_account_id, window_started_at, window_ended_at, rate_card_version_id, usage_snapshot_hash)`.  An unknown version, a cross-tenant version, or a missing metric fails closed and does not invent a price.  An identical replay returns the same `rating_run_id` and totals.  Rating never drafts an invoice, never calls a payment provider, and never writes a posted journal.

## Invoice draft

`metering_billing.InvoiceDraftService` copies one stored rating run into an append-only invoice-intent draft.  Identity is `(tenant_account_id, rating_run_id)` plus the rating run's usage-snapshot hash.  An identical replay returns the same `invoice_draft_id` and exact totals.  Status is `draft` only.  The draft is a commercial document, not revenue recognition and not a posted journal.

## Accounting export

`metering_billing.AccountingExportService` copies one stored invoice draft or one stored payment receipt into an append-only `accounting_journal_proposal`.  Draft identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)`.  Cash identity is `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)`.  Credit identity is `(tenant_account_id, credit_adjustment_id, source_payload_hash, proposal_contract_version)`.  An identical replay returns the same `proposal_id`.  Untaxed draft lines debit `accounts_receivable` and credit `usage_revenue`.  Taxed draft lines debit `accounts_receivable` for the inclusive amount, credit `usage_revenue` for the exclusive amount, and credit `tax_payable` for the tax amount.  Cash lines debit `cash_receipt` and credit `accounts_receivable`.  Credit lines debit `usage_revenue` and credit `accounts_receivable`.  Status stays inside the proposal lifecycle and is never `posted`.  AIS next pulls validated proposals.  This path does not open fiscal periods, resolve statutory account IDs, change collection outstanding, call a payment provider, or flip `proposal_status` after a pull.

## Collection case

`metering_billing.CollectionCaseService` opens an append-only commercial collection case from one stored invoice draft.  Identity is `(tenant_account_id, invoice_draft_id)`.  Outstanding equals the exact draft total.  An identical replay returns the same `collection_case_id`.  Status stays `open` or `dunning` until a receipt settles remaining outstanding to zero.  Dunning events are commercial reminders (`first_notice`, `overdue_notice`) and do not capture money or change AIS books.

## Payment intent

`metering_billing.PaymentIntentService` projects one provider-neutral `payment_intent` from a stored collection case.  Identity is `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  The hash covers the case outstanding, currency, and stored status snapshot.  Amount equals the exact case outstanding.  An identical replay returns the same `payment_intent_id`.  Status stays `projected`, `cancelled`, or `rejected` and is never `captured`, `settled`, or `posted`.  This path does not store a card PAN, call a named provider, change collection outstanding, or post a journal.  The operator next records a commercial receipt or cancels the intent.

## Payment settlement

`metering_billing.PaymentSettlementService` applies one commercial `payment_receipt` against a stored projected payment intent.  Identity is `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  The hash covers the intent amount, currency, status, and received amount.  Receipt status is `applied` only.  The linked collection-case outstanding is reduced by the same exact amount; remaining zero marks the case `settled`.  An identical replay returns the same `payment_receipt_id`.  `cancel_payment_intent` flips a projected intent to `cancelled` without writing a receipt or changing outstanding.  This path does not store a card PAN, call a named provider, emit an `accounting_journal_proposal`, or post a journal.  The operator next proposes a cash journal to AIS, or records another partial receipt.

## HTTP accept surface

`metering_billing.http_app.create_http_app` is a stdlib WSGI adapter.  It parses JSON, requires `tenant_reference` on every write, calls the existing in-process services, and returns each `as_contract_dict` result.  Standalone serving is `python -m metering_billing.http_app` on `0.0.0.0:$PORT`.  HTTP 200 means `accepted` or `duplicate_replay` on writes, or a successful read.  HTTP 422 means `rejected` or an unreadable request.  HTTP 404 is an unknown route or an unknown/cross-tenant proposal, credit, observation, or rate-card version.  Money stays exact-decimal strings.  The adapter never posts a journal, never stores a card PAN, and never calls Stripe, Adyen, or Toss.

`GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}` are the AIS pull.  Tenant is required via optional `X-CWL-Tenant-Reference` or `tenant_reference` in the query or JSON body.  If both are present they must match.  Optional filters are `proposal_status`, inclusive `proposed_after`, and a bounded `cursor` / `page_limit`.  List items are the published journal-proposal contract with semantic account roles only.  Cash, AR, and credit proposals share `journal_proposal` and appear in the same list.  Query does not mutate `proposal_status`.  AIS pulls validated proposals and later returns `posting_receipt`.

## Posting-receipt observation

`metering_billing.PostingReceiptPullService` GETs an AIS-owned `posting_receipt` from `{ais_base_url}/posting-receipts?idempotency_key=` with required `X-CWL-Tenant-Reference`.  The response is validated against the consumed AIS contract in `schemas/consumed/`.  A successful pull persists one append-only `posting_receipt_observation` identified by `(tenant_account_id, idempotency_key)` plus `source_payload_hash` / `receipt_id`.  AIS `receipt_id` is not the internal primary key.  Replay of the same tenant, key, and receipt returns the stored observation.  `posting_status_code` stays an AIS fact (`posted`, `held`, `rejected`, `reversed`) and is never mapped onto Billing `proposal_status`.  AIS 403 writes zero rows.  AIS 404 is `not_yet_accepted` and writes zero rows.  `POST /v1/posting-receipt-observations` triggers the pull.  `GET /v1/posting-receipt-observations/{idempotency_key}` reads a stored observation and does not call AIS.

## Tax assessment

`metering_billing.TaxRateService` publishes one tenant-scoped `tax_rate_schedule` and one immutable `tax_rate_version`.  Header identity is `(tenant_account_id, tax_code)`.  Version identity is `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)`.  Closed codes are `vat`, `gst`, and `sales_tax`.  `tax_rate` is an exact `Decimal` in `[0, 1]`.  `metering_billing.TaxAssessmentService` applies a persisted version to one invoice draft.  Identity is `(tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version)`.  `tax_amount` is half-even rounded to the documented ISO 4217 minor units.  Assess after a collection case is open fails closed.  `propose_journal` then debits `accounts_receivable` inclusive, credits `usage_revenue` exclusive, and credits `tax_payable` tax.  Collection outstanding uses the inclusive amount when an assessment exists.  AIS must map `tax_payable`.  This path does not call an OSS engine, store exemptions, or start operator UI.

## Credit adjustment

`metering_billing.CreditAdjustmentService` records one commercial `credit_adjustment` against a stored invoice draft.  Identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)`.  `credit_amount` is an exact `Decimal` greater than zero and cannot exceed remaining adjustable consideration.  Currency is copied from the draft.  Closed reasons are `rating_correction`, `goodwill`, and `billing_error`.  If a collection case exists, outstanding is reduced by the same amount; remaining zero marks the case `settled`.  The service emits one validated journal proposal that debits `usage_revenue` and credits `accounts_receivable`.  An identical replay returns the same `credit_adjustment_id` and `proposal_id`.  `POST /v1/credit-adjustments` records the credit.  `GET /v1/credit-adjustments/{credit_adjustment_id}` is tenant-scoped and does not call AIS.  Operators record the credit, then let AIS pull the validated proposal.  This path does not post, open a fiscal period, emit statutory account IDs, invent a new account role, or start tax, refund-to-card, or chargeback.

## Failure policy

- Duplicate input returns the existing receipt.
- A source-event key replay with a different payload hash or contract version is a conflict.
- Attribution URNs that leave the event tenant are rejected.
- Invalid quality or meter configuration fails closed.
- Provider timeouts retain internal facts and retry idempotently.
- Existing provider objects never fail over automatically to a different provider.
- Accounting rejection does not rewrite billing facts; it creates a reconciliation exception.
