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

## Commercial rating

`metering_billing.UsageRatingService` is the read-and-rate path for already-stored usage.  A buyer supplies a tenant, a half-open ISO 8601 window, and a rate-card version.  The service aggregates billable quality only, multiplies exact quantities by exact unit prices, and persists append-only `rating_run` and `rating_line` rows.  Identity is `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)`.  An identical replay returns the same `rating_run_id` and totals.  Rating never drafts an invoice, never calls a payment provider, and never writes a posted journal.

## Invoice draft

`metering_billing.InvoiceDraftService` copies one stored rating run into an append-only invoice-intent draft.  Identity is `(tenant_account_id, rating_run_id)` plus the rating run's usage-snapshot hash.  An identical replay returns the same `invoice_draft_id` and exact totals.  Status is `draft` only.  The draft is a commercial document, not revenue recognition and not a posted journal.

## Accounting export

`metering_billing.AccountingExportService` copies one stored invoice draft or one stored payment receipt into an append-only `accounting_journal_proposal`.  Draft identity is `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)`.  Cash identity is `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)`.  An identical replay returns the same `proposal_id`.  Draft lines debit `accounts_receivable` and credit `usage_revenue`.  Cash lines debit `cash_receipt` and credit `accounts_receivable`.  Status stays inside the proposal lifecycle and is never `posted`.  The operator next hands the proposal to the Accounting Information Platform.  This path does not open fiscal periods, resolve statutory account IDs, change collection outstanding, or call a payment provider.

## Collection case

`metering_billing.CollectionCaseService` opens an append-only commercial collection case from one stored invoice draft.  Identity is `(tenant_account_id, invoice_draft_id)`.  Outstanding equals the exact draft total.  An identical replay returns the same `collection_case_id`.  Status stays `open` or `dunning` until a receipt settles remaining outstanding to zero.  Dunning events are commercial reminders (`first_notice`, `overdue_notice`) and do not capture money or change AIS books.

## Payment intent

`metering_billing.PaymentIntentService` projects one provider-neutral `payment_intent` from a stored collection case.  Identity is `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  The hash covers the case outstanding, currency, and stored status snapshot.  Amount equals the exact case outstanding.  An identical replay returns the same `payment_intent_id`.  Status stays `projected`, `cancelled`, or `rejected` and is never `captured`, `settled`, or `posted`.  This path does not store a card PAN, call a named provider, change collection outstanding, or post a journal.  The operator next records a commercial receipt or cancels the intent.

## Payment settlement

`metering_billing.PaymentSettlementService` applies one commercial `payment_receipt` against a stored projected payment intent.  Identity is `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  The hash covers the intent amount, currency, status, and received amount.  Receipt status is `applied` only.  The linked collection-case outstanding is reduced by the same exact amount; remaining zero marks the case `settled`.  An identical replay returns the same `payment_receipt_id`.  `cancel_payment_intent` flips a projected intent to `cancelled` without writing a receipt or changing outstanding.  This path does not store a card PAN, call a named provider, emit an `accounting_journal_proposal`, or post a journal.  The operator next proposes a cash journal to AIS, or records another partial receipt.

## HTTP accept surface

`metering_billing.http_app.create_http_app` is a stdlib WSGI adapter.  It parses JSON, requires `tenant_reference` on every write, calls the existing in-process services, and returns each `as_contract_dict` result.  Standalone serving is `python -m metering_billing.http_app` on `0.0.0.0:$PORT`.  HTTP 200 means `accepted` or `duplicate_replay`.  HTTP 422 means `rejected` or an unreadable request.  HTTP 404 is only an unknown route.  Money stays exact-decimal strings.  The adapter never posts a journal, never stores a card PAN, and never calls Stripe, Adyen, or Toss.  AIS remains the consumer of journal proposals and later posting receipts.

## Failure policy

- Duplicate input returns the existing receipt.
- A source-event key replay with a different payload hash or contract version is a conflict.
- Attribution URNs that leave the event tenant are rejected.
- Invalid quality or meter configuration fails closed.
- Provider timeouts retain internal facts and retry idempotently.
- Existing provider objects never fail over automatically to a different provider.
- Accounting rejection does not rewrite billing facts; it creates a reconciliation exception.
