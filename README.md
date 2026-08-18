# Metering Billing Platform

CWL's provider-neutral commercial control plane for usage attribution, metering, rating, entitlements, invoice intent, payment-provider projection, and reconciliation.

## Authority

This repository owns commercial usage and billing truth. It does **not** own the statutory chart of accounts, legal books, posted journals, fiscal close, consolidation, or financial statements. Those belong to a separate Accounting Information Platform.

```text
CWL products
  -> canonical usage events
  -> Metering Billing Platform
  -> invoice and settlement facts
  -> accounting journal proposals
  -> Accounting Information Platform
  -> posted journals and financial statements
```

## Initial foundation

The current milestone contains:

- closed JSON Schema contracts for usage events, usage-event presentment, provider capabilities, usage-ingestion receipts, rating runs, rating-run presentment, invoice drafts, collection cases, collection-case presentment, collection-aging presentment, payment intents, payment-intent presentment, payment receipts, payment-receipt presentment, unapplied cash, unapplied-cash presentment, unapplied-cash applications, unapplied-cash-application presentment, credit adjustments, credit-adjustment presentment, issued credit notes, issued-credit-note presentment, credit-note applications, credit-note-application presentment, collection-case settlements, collection-case-settlement presentment, rate cards, rate-card presentment, tax rates, tax assessments, tax-assessment presentment, posting-receipt observation presentment, tenant API credentials, webhook subscriptions, webhook deliveries, AIS outbox drains, and semantically validated accounting journal proposals, plus a consumed AIS posting-receipt contract;
- a normalized PostgreSQL 18 core plus usage-identity, rating-run, invoice-draft, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, rate-card-catalog, tax-assessment, credit-tax-unwind, tenant-api-credential, webhook-outbox, issued-invoice, and issued-credit-note migrations with tenant-scoped attribution constraints;
- an importable `metering_billing` package that ingests immutable usage, publishes versioned rate cards, rates tenant-scoped half-open windows against a persisted version, drafts invoice intent, issues commercial invoices, issues commercial credit notes, publishes tax rates, assesses tax on a draft, emits proposal-only journals, opens commercial collection cases, presents collection aging, projects provider-neutral payment intents, applies commercial payment receipts, records commercial credits, pulls AIS posting receipts as observations, drains AIS posting-receipt outbox events, issues tenant API credentials, registers webhook callbacks for accepted commercial facts, and accepts those writes over a stdlib HTTP adapter;
- an importable `operator_console` Storybook that renders the invoice-draft, issued-invoice, issued-credit-note, collection-case, collection-aging, payment-intent, payment-receipt, credit-adjustment, rate-card, usage-event, rating-run, tax-assessment, and posting-receipt-observation presentment contracts with design tokens and exact-decimal fixtures;
- explicit billing-versus-accounting boundaries;
- offline repository validation with 100% line and branch coverage;
- exact-head CI with commit-pinned actions.

## Run validation

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements-quality.txt
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=100 --show-missing
python3 scripts/validate_repository.py
```

## Ingest usage

```bash
python3 -c "from metering_billing import UsageIngestionService, MemoryUsageLedger"
# POST /v1/usage-events
# GET /v1/usage-events/{usage_event_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/usage-events?tenant_reference=urn:cwl:tenant_001
```

Register the tenant, billing account, principal, meter, and quality rules on a `MemoryUsageLedger`, then call `UsageIngestionService.ingest_usage_batch`. Identical retries return `duplicate_replay` and leave the stored usage set unchanged. A changed hash or contract version for the same source key is rejected. `POST /v1/usage-events` stays that ingest and refuses PAN and provider secrets. After a `usage_event` exists, `GET /v1/usage-events/{usage_event_id}` returns the tenant-scoped statement. `GET /v1/usage-events` lists `{usage_events, next_cursor}`. Ingest usage, then rate a window against a published card.

## Publish a rate card

```bash
python3 -c "from metering_billing import RateCardService"
```

Call `RateCardService.publish_rate_card` with a tenant, a two-or-more-word `snake_case` card name, an ISO currency, and one or more flat metric lines. Each `unit_amount` is an exact decimal greater than zero. The same tenant, name, canonical line hash, and contract version return `duplicate_replay`. A later distinct line set increments the version. A published version is never edited.

## Rate a usage window

```bash
python3 -c "from metering_billing import TimeWindow, UsageRatingService"
# POST /v1/rating-runs
# GET /v1/rating-runs/{rating_run_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/rating-runs?tenant_reference=urn:cwl:tenant_001
```

Publish a rate card, then call `UsageRatingService.rate_usage_window` with a tenant, a half-open ISO 8601 window, and that persisted rate-card version. Only `meter_quality_rule` billable quality enters the invoice-intent total. Rating uses the stored `unit_amount` for the matching `metric_code`. An unknown version, a cross-tenant version, or a missing metric fails closed. An identical replay returns the same `rating_run_id` and exact totals. `POST /v1/rating-runs` stays that command and refuses PAN and provider secrets. After a `rating_run` exists, `GET /v1/rating-runs/{rating_run_id}` returns the tenant-scoped statement. `GET /v1/rating-runs` lists `{rating_runs, next_cursor}`. Rate a window, then draft an invoice. Rating does not draft an invoice, call a payment provider, or post a journal.

## Draft an invoice

```bash
python3 -c "from metering_billing import InvoiceDraftService"
```

After a `rating_run` exists, call `InvoiceDraftService.draft_invoice` with the tenant and `rating_run_id`. The draft total equals the rating-run billable total. An identical replay returns the same `invoice_draft_id`. Status stays `draft`. The draft does not issue, collect, call a payment provider, or post a journal.

After an `invoice_draft` exists, call `InvoicePresentmentService.present_invoice_draft` or `GET /v1/invoice-drafts/{invoice_draft_id}` with the tenant. The statement shows exclusive, tax, inclusive, credited, and amount due as exact-decimal strings. Tax is zero when no assessment exists. Amount due is inclusive minus accepted credits and never below zero. `GET /v1/invoice-drafts` lists summaries. Open the draft statement, then collect or credit. The read does not post or call AIS.

After an `invoice_draft` exists, call `IssuedInvoiceService.issue_invoice` or `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` with the tenant. Replay of the same tenant and draft returns the same `issued_invoice_id`. Totals freeze the draft or its tax assessment. First successful issue enqueues one existing `invoice.issued` webhook outbox event. `GET /v1/issued-invoices/{issued_invoice_id}` and `GET /v1/issued-invoices` present the snapshot. Issue invoice, then collect or credit. The path does not invent statutory numbering, capture payment, or call AIS.

After a `credit_adjustment` exists, call `IssuedCreditNoteService.issue_credit_note` or `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` with the tenant. Replay of the same tenant and credit returns the same `issued_credit_note_id`. Totals freeze the stored exclusive, tax, and inclusive credit amounts. First successful issue enqueues one existing `credit_note.issued` webhook outbox event. `GET /v1/issued-credit-notes/{issued_credit_note_id}` and `GET /v1/issued-credit-notes` present the snapshot. Issue the credit note; the validated journal remains available for AIS. The path does not invent statutory numbering, capture payment, or call AIS.

After an `issued_credit_note` and an open `collection_case` exist for the same invoice, call `CreditNoteApplicationService.apply_credit_note` or `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` with the tenant and `issued_credit_note_id`. Replay of the same tenant and issued credit note returns the same `credit_note_application_id` and never double-reduces outstanding. First successful apply enqueues one existing `credit_note.applied` webhook outbox event. `GET /v1/credit-note-applications/{credit_note_application_id}` and `GET /v1/credit-note-applications` present the stored application. Apply the issued credit note, then collect the residual. The path does not invent a journal, tax unwind, second webhook system, statutory numbering, write-off, settlement, capture payment, or call AIS.

After an open `collection_case` already shows exact-zero outstanding, call `CollectionCaseSettlementService.settle_collection_case` or `POST /v1/collection-cases/{collection_case_id}/settlements`. Replay of the same tenant and case returns the same `collection_case_settlement_id` and never double-settles. First successful settle enqueues one existing `collection.settled` webhook outbox event. `GET /v1/collection-case-settlements/{collection_case_settlement_id}` and `GET /v1/collection-case-settlements` present the stored settlement. Settle the zero-outstanding case, then wait. The path does not invent a journal, tax unwind, second webhook system, statutory numbering, write-off, payment receipt, or AIS call.

After an open `collection_case` still shows leftover remaining, call `CollectionWriteOffService.write_off_collection_case` or `POST /v1/collection-cases/{collection_case_id}/write-offs`. Replay of the same tenant and case returns the same `collection_write_off_id` and never re-zeros outstanding. Remaining becomes exact zero without settling. First successful write-off enqueues one existing `write_off.recorded` webhook outbox event. `GET /v1/collection-write-offs/{collection_write_off_id}` and `GET /v1/collection-write-offs` present the stored write-off. After the write-off exists, `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals` composes one validated write-off/AR journal for AIS to pull. Write off leftover remaining, compose the journal, then settle. The path does not invent a tax unwind, second webhook system, statutory numbering, payment receipt, credit note, settlement command, or AIS call.

## Propose a journal

```bash
python3 -c "from metering_billing import AccountingExportService"
```

After an `invoice_draft` exists, call `AccountingExportService.propose_journal` with the tenant and `invoice_draft_id`. After a `payment_receipt` exists, the receipt write already proposed the cash journal; `AccountingExportService.propose_cash_journal` remains a manual replay. After a `credit_adjustment` exists, credit accept already proposed the credit journal; `AccountingExportService.propose_credit_journal` or `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals` remains an explicit compose or replay. After a `collection_write_off` exists, call `AccountingExportService.propose_write_off_journal` or `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals`. After an `unapplied_cash_refund` exists, call `AccountingExportService.propose_refund_journal` or `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals`. After parked `unapplied_cash` exists, call `AccountingExportService.propose_unapplied_cash_journal` or `POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals`. Each proposal is one balanced exact-decimal `accounting_journal_proposal` that uses semantic account roles and an intended book role. An identical replay returns the same `proposal_id`. Status stays inside the proposal lifecycle and is never `posted`. Cash lines debit `cash_receipt` and credit `accounts_receivable`. Credit lines debit `usage_revenue` and credit `accounts_receivable`. Write-off lines debit `write_off_expense` and credit `accounts_receivable`. Refund lines debit `unapplied_cash` and credit `cash_receipt`. Leftover-park lines debit `cash_receipt` and credit `unapplied_cash`.

## Open a collection case

```bash
python3 -c "from metering_billing import CollectionCaseService"
```

After an `invoice_draft` exists, call `CollectionCaseService.open_collection_case` with the tenant and `invoice_draft_id`. Outstanding equals the exact draft total. An identical replay returns the same `collection_case_id`. Status stays `open` or `dunning`. Then call `record_dunning_event` with `first_notice` or `overdue_notice`. Reminders do not capture money or post journals.

## Project a payment intent

```bash
python3 -c "from metering_billing import PaymentIntentService"
```

After a `collection_case` exists, call `PaymentIntentService.project_payment_intent` with the tenant and `collection_case_id`, or `POST /v1/payment-intents`. The intent amount equals the exact case outstanding. An identical replay returns the same `payment_intent_id`. Status stays `projected`. The intent does not capture, settle, store a card PAN, or post a journal.

## Present a payment intent

```bash
python3 -c "from metering_billing import PaymentIntentPresentmentService"
# GET /v1/payment-intents/{payment_intent_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/payment-intents?tenant_reference=urn:cwl:tenant_001
```

After a `payment_intent` exists, `GET /v1/payment-intents/{payment_intent_id}` returns the tenant-scoped statement. Create a projected payment intent, then record the receipt.

## Record a payment receipt

```bash
python3 -c "from metering_billing import PaymentSettlementService"
```

After a projected `payment_intent` exists, call `PaymentSettlementService.record_payment_receipt` with the tenant, `payment_intent_id`, and exact `received_amount`, or `POST /v1/payment-receipts`. The receipt status is `applied`. The linked collection-case outstanding is reduced by the same amount; remaining zero marks the case `settled`. An identical replay returns the same `payment_receipt_id`. Accept also proposes the existing cash journal. Call `cancel_payment_intent` to flip a projected intent to `cancelled` without writing a receipt. The receipt does not capture via a provider or post a journal. `POST /v1/cash-journal-proposals` remains a manual replay.

## Present a payment receipt

```bash
python3 -c "from metering_billing import PaymentReceiptPresentmentService"
# GET /v1/payment-receipts/{payment_receipt_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/payment-receipts?tenant_reference=urn:cwl:tenant_001
```

After a `payment_receipt` exists, `GET /v1/payment-receipts/{payment_receipt_id}` returns the tenant-scoped statement. Record the receipt; the cash journal is already validated for AIS to pull.

```http
# POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash
# GET /v1/unapplied-cash/{unapplied_cash_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/unapplied-cash?tenant_reference=urn:cwl:tenant_001
```

After a stored `payment_receipt` exists, `POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash` parks leftover remittance without rewriting #12 overpay rejection. Replay of the same tenant and receipt returns the same `unapplied_cash_id`. `GET /v1/unapplied-cash/{unapplied_cash_id}` returns the tenant-scoped statement. Do not auto-apply leftover to another case. The path does not invent a journal, webhook, write-off, settlement, credit note, or AIS call.

```http
# POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications
# GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/unapplied-cash-applications?tenant_reference=urn:cwl:tenant_001
```

After leftover is parked, `POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications` applies the full parked amount onto another open case. Replay of the same tenant and leftover returns the same `unapplied_cash_application_id`. First successful apply enqueues one existing `unapplied_cash.applied` webhook outbox event. Remaining zero does not settle; #46 remains the explicit settle-when-zero command. `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` returns the tenant-scoped statement. The path does not invent a journal, write-off, credit note, AIS call, settlement command, or second webhook system.
After leftover is parked and unused, `POST /v1/unapplied-cash/{unapplied_cash_id}/refunds` records a commercial refund of the full parked amount. Replay of the same tenant and leftover returns the same `unapplied_cash_refund_id`. First successful refund enqueues one existing `refund.recorded` webhook outbox event. The parked leftover row stays `parked`. `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` returns the tenant-scoped statement. After the refund exists, `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals` composes one validated unapplied-cash/cash journal for AIS to pull. The path does not invent a write-off, settlement, credit note, PSP capture, AIS call, statutory numbering, or second webhook system.

## Present a credit adjustment

```bash
python3 -c "from metering_billing import CreditAdjustmentPresentmentService"
# GET /v1/credit-adjustments/{credit_adjustment_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/credit-adjustments?tenant_reference=urn:cwl:tenant_001
```

After a `credit_adjustment` exists, `GET /v1/credit-adjustments/{credit_adjustment_id}` returns the tenant-scoped statement. Record the credit; AIS pulls the validated journal.

## Accept writes over HTTP

```bash
python3 -c "from metering_billing.http_app import create_http_app"
python3 -m metering_billing.http_app
```

`create_http_app(ledger=...)` is a thin stdlib WSGI adapter over the services above. Standalone serving binds `0.0.0.0:$PORT` (default 8000). Every write requires `tenant_reference`. Money stays exact-decimal strings. HTTP 200 means `accepted` or `duplicate_replay` on writes, or a successful read. HTTP 422 means `rejected` or an unreadable request. HTTP 404 is an unknown route or an unknown/cross-tenant proposal, credit, observation, rate-card version, usage event, rating run, invoice draft, collection case, payment intent, payment receipt, or API credential. The adapter does not post journals or call a named payment provider.

Until a tenant has an active API credential, the existing tenant pin is enough (bootstrap window). AIS can keep pulling with `X-CWL-Tenant-Reference` until a key is issued for that tenant. After a key exists, send it on every `/v1` call.

## Issue a tenant API credential

```bash
python3 -c "from metering_billing import TenantApiCredentialService"
# POST /v1/tenant-api-credentials
# GET /v1/tenant-api-credentials/{tenant_api_credential_id}
# GET /v1/tenant-api-credentials
# POST /v1/tenant-api-credentials/{id}/revoke
```

Call `TenantApiCredentialService.issue_credential` with a tenant and an optional two-or-more-word `snake_case` `credential_label`. The response includes the secret once. The ledger stores only a keyed HMAC. A second issue always mints a new secret. After one or more active keys exist, every `/v1` write and GET except credential issue requires `Authorization: Bearer <secret>` or `X-CWL-Api-Key: <secret>` whose tenant equals `X-CWL-Tenant-Reference` / `tenant_reference`. `GET /v1/tenant-api-credentials/{tenant_api_credential_id}` and `GET /v1/tenant-api-credentials` present stored metadata as `{tenant_api_credentials, next_cursor}` and never the secret or hash. `GET /healthz` stays unauthenticated. Issue a key, then send it on every `/v1` call; revoke when leaked.

## Register a webhook callback

```bash
python3 -c "from metering_billing import WebhookSubscriptionService, WebhookDeliveryService"
# POST /v1/webhook-subscriptions
# GET /v1/webhook-subscriptions
# GET /v1/webhook-subscriptions/{webhook_subscription_id}
# POST /v1/webhook-subscriptions/{id}/revoke
# POST /v1/webhook-deliveries
# GET /v1/webhook-outbox-events/{outbox_event_id}
# GET /v1/webhook-outbox-events
# GET /v1/webhook-deliveries/{delivery_attempt_id}
# GET /v1/webhook-deliveries
```

Register an https callback, then run deliveries; AIS may keep polling. `WebhookSubscriptionService.register_subscription` accepts a tenant, https `callback_url`, and a closed event-type set (`journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, `credit_note.issued`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`). http is allowed only for localhost tests. The secret is returned once. Replay of the same tenant, URL, event set, and contract version returns the same `webhook_subscription_id`. `WebhookSubscriptionPresentmentService` projects stored metadata. `GET /v1/webhook-subscriptions/{webhook_subscription_id}` and `GET /v1/webhook-subscriptions` present `{webhook_subscriptions, next_cursor}` and never return the secret, hash, prefix, or signed body. Accepted commercial facts append `webhook_outbox_event` rows. `WebhookOutboxEventPresentmentService` projects stored commercial outbox metadata. `GET /v1/webhook-outbox-events/{outbox_event_id}` and `GET /v1/webhook-outbox-events` present `{webhook_outbox_events, next_cursor}` and never return `payload_json`, the webhook secret, hash, prefix, or signature. `POST /v1/webhook-deliveries` POSTs the envelope and signs the raw body with `X-CWL-Webhook-Signature: sha256=<hex>`. `GET /v1/webhook-deliveries/{delivery_attempt_id}` and `GET /v1/webhook-deliveries` present stored `webhook_delivery_attempt` rows as `{webhook_deliveries, next_cursor}` and never resend or return the secret, hash, or signed body. This path does not flip `proposal_status` or call AIS posting-receipt.

## Present an invoice draft

```bash
python3 -c "from metering_billing import InvoicePresentmentService"
# GET /v1/invoice-drafts/{invoice_draft_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/invoice-drafts?tenant_reference=urn:cwl:tenant_001
```

After an `invoice_draft` exists, `GET /v1/invoice-drafts/{invoice_draft_id}` returns the tenant-scoped statement. Open the draft statement, then collect or credit.

## Issue a commercial invoice

```bash
python3 -c "from metering_billing import IssuedInvoiceService, IssuedInvoicePresentmentService"
# POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices
# GET /v1/issued-invoices/{issued_invoice_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-invoices?tenant_reference=urn:cwl:tenant_001
```

After an `invoice_draft` exists, `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` writes one immutable commercial snapshot. `GET /v1/issued-invoices/{issued_invoice_id}` returns the tenant-scoped statement. Issue invoice, then collect or credit.

## Issue a commercial credit note

```bash
python3 -c "from metering_billing import IssuedCreditNoteService, IssuedCreditNotePresentmentService"
# POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes
# GET /v1/issued-credit-notes/{issued_credit_note_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-credit-notes?tenant_reference=urn:cwl:tenant_001
```

After a `credit_adjustment` exists, `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` writes one immutable commercial snapshot. `GET /v1/issued-credit-notes/{issued_credit_note_id}` returns the tenant-scoped statement. Issue the credit note; the validated journal remains available for AIS.

## Apply an issued credit note to a collection case

```bash
python3 -c "from metering_billing import CreditNoteApplicationService, CreditNoteApplicationPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/credit-note-applications
# GET /v1/credit-note-applications/{credit_note_application_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/credit-note-applications?tenant_reference=urn:cwl:tenant_001
```

After an `issued_credit_note` and an open `collection_case` exist for the same invoice, `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` reduces outstanding by the exact issued inclusive amount. First successful apply enqueues one `credit_note.applied` outbox event. `GET /v1/credit-note-applications/{credit_note_application_id}` returns the tenant-scoped statement. Apply the issued credit note, then collect the residual.

## Settle a zero-outstanding collection case

```bash
python3 -c "from metering_billing import CollectionCaseSettlementService, CollectionCaseSettlementPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/settlements
# GET /v1/collection-case-settlements/{collection_case_settlement_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-case-settlements?tenant_reference=urn:cwl:tenant_001
```

After an open `collection_case` already shows exact-zero outstanding, `POST /v1/collection-cases/{collection_case_id}/settlements` flips status to `settled` without inventing a receipt or write-off. First successful settle enqueues one `collection.settled` outbox event. `GET /v1/collection-case-settlements/{collection_case_settlement_id}` returns the tenant-scoped statement. Settle the zero-outstanding case, then wait.

## Write off leftover collection remaining

```bash
python3 -c "from metering_billing import CollectionWriteOffService, CollectionWriteOffPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/write-offs
# GET /v1/collection-write-offs/{collection_write_off_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-write-offs?tenant_reference=urn:cwl:tenant_001
```

After an open `collection_case` still shows leftover remaining, `POST /v1/collection-cases/{collection_case_id}/write-offs` zeros remaining without inventing a receipt, credit, or settlement. First successful write-off enqueues one `write_off.recorded` outbox event. `GET /v1/collection-write-offs/{collection_write_off_id}` returns the tenant-scoped statement. `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals` composes one validated write-off/AR journal for AIS to pull. Write off leftover remaining, compose the journal, then settle.

## Present a collection case

```bash
python3 -c "from metering_billing import CollectionCasePresentmentService"
# GET /v1/collection-cases/{collection_case_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-cases?tenant_reference=urn:cwl:tenant_001
```

After a `collection_case` exists, `GET /v1/collection-cases/{collection_case_id}` returns the tenant-scoped statement. Open the collection case, then collect or credit.

## Present collection aging

```bash
python3 -c "from metering_billing import CollectionAgingPresentmentService"
# GET /v1/collection-aging?tenant_reference=urn:cwl:tenant_001
```

After open or dunning `collection_case` rows exist, `GET /v1/collection-aging` returns current / 1-30 / 31-60 / 61-90 / 90+ outstanding grouped by `currency_code`. Settled cases and exact-zero remaining are omitted. Open the aging statement, then collect or credit. This path does not invent a journal, write-off, settlement, payment, or AIS call.

## Present a dunning notice

```bash
python3 -c "from metering_billing import DunningEventPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/dunning-events
# GET /v1/dunning-events/{dunning_event_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/dunning-events?tenant_reference=urn:cwl:tenant_001
```

After a `collection_dunning_event` exists, `GET /v1/dunning-events/{dunning_event_id}` returns the tenant-scoped reminder. `GET /v1/dunning-events` lists `{dunning_events, next_cursor}`. Record the commercial reminder, then collect or credit. This path does not send mail or invent recipient PII.

## Present a statement in Storybook

```bash
cd operator_console
npm install
npm run storybook
```

`operator_console` renders the #21 invoice statement, the issued-invoice statement, the issued-credit-note statement, the credit-note-application statement, the collection-case-settlement statement, the collection-case statement, the payment-intent statement, the payment-receipt statement, the credit-adjustment statement, the rate-card statement, the usage-event statement, the rating-run statement, the tax-assessment statement, and the posting-receipt observation statement with tokenized status chip and tenant pin. Amounts stay exact-decimal strings. Customer copy on a collection-case settlement is: settle the zero-outstanding case, then wait. Storybook is the UI surface for this slice. The package is importable and does not replace `metering_billing`.

## Pull journal proposals

```bash
# GET /v1/journal-proposals?tenant_reference=urn:cwl:tenant_001
# GET /v1/journal-proposals/{proposal_id}?tenant_reference=urn:cwl:tenant_001
```

AIS pulls validated proposals from the same stdlib app. Pin the tenant with optional `X-CWL-Tenant-Reference` or with `tenant_reference` in the query or JSON body. If both are present they must match. That pull keeps working until a key is issued for the tenant; after issue, send the key on every `/v1` call. Cash, AR, and credit proposals share `journal_proposal` and appear in the same list. Query never marks a proposal exported, posted, or consumed. Billing emits semantic account roles only; AIS maps `cash_receipt` to its own chart.

## Pull a posting receipt

```bash
python3 -c "from metering_billing import PostingReceiptPullService"
# POST /v1/posting-receipt-observations
# GET /v1/posting-receipt-observations/{idempotency_key}?tenant_reference=urn:cwl:tenant_001
# GET /v1/posting-receipt-observations?tenant_reference=urn:cwl:tenant_001
```

After AIS accepts a validated proposal, an operator pulls the AIS `posting_receipt` and Billing stores it as a commercial observation. If AIS returns 404, accept the proposal on AIS and retry. `posting_status_code` stays an AIS fact. Billing `proposal_status` stays `validated`. `GET /v1/posting-receipt-observations/{idempotency_key}` stays the existing #16 item read and does not call AIS. `GET /v1/posting-receipt-observations` lists `{posting_receipt_observations, next_cursor}`. Drain AIS outbox, then store the receipt observation.

## Drain the AIS outbox

```bash
python3 -c "from metering_billing import AisOutboxDrainService"
# POST /v1/ais-outbox-drains
```

Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty. `AisOutboxDrainService.drain_ais_outbox` GETs `GET /outbox-events?event_type_code=posting_receipt`, matches constructed `urn:cwl:accounting:posting_receipt:{proposal_id}` and `urn:cwl:accounting:general_journal:{proposal_id}` by equality, then GETs `/posting-receipts?idempotency_key=` with the stored Billing key. Empty unpublished pages skip receipt GETs. After a stored observation, POST `/outbox-events/{outbox_event_id}/publish`. This path does not parse the payload URN, drain `journal_reversal` or `period_close`, or flip `proposal_status`.

## Record a credit adjustment

```bash
python3 -c "from metering_billing import CreditAdjustmentService"
# POST /v1/credit-adjustments
# GET /v1/credit-adjustments/{credit_adjustment_id}
```

After an `invoice_draft` exists, call `CreditAdjustmentService.record_credit_adjustment` with the tenant, `invoice_draft_id`, exact `credit_amount`, and a closed `credit_reason_code` (`rating_correction`, `goodwill`, or `billing_error`), or `POST /v1/credit-adjustments`. The credit cannot exceed remaining adjustable consideration. If a collection case exists, outstanding is reduced by the same inclusive amount; remaining zero marks the case `settled`. When a tax assessment exists, the credit is split proportionally and the journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`. Untaxed credits stay two-line. An identical replay returns the same `credit_adjustment_id` and `proposal_id`. Record the credit; AIS pulls the validated journal. This path does not post, call AIS, refund-to-card, or chargeback.

## Publish a tax rate and assess a draft

```bash
python3 -c "from metering_billing import TaxRateService, TaxAssessmentService"
# POST /v1/tax-assessments
# GET /v1/tax-assessments/{tax_assessment_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/tax-assessments?tenant_reference=urn:cwl:tenant_001
```

Call `TaxRateService.publish_tax_rate` with a tenant, a closed `tax_code` (`vat`, `gst`, or `sales_tax`), and an exact `tax_rate` in `[0, 1]`. Then call `TaxAssessmentService.assess_tax` with the tenant, `invoice_draft_id`, and that persisted version, or `POST /v1/tax-assessments`. Tax is half-even rounded to the documented ISO 4217 minor units. Assess before opening a collection case. A taxed `propose_journal` credits semantic `tax_payable`; AIS must map that role. After a `tax_assessment` exists, `GET /v1/tax-assessments/{tax_assessment_id}` stays the existing #19 item read. `GET /v1/tax-assessments` lists `{tax_assessments, next_cursor}`. Publish a tax rate, assess the draft, then propose the journal and let AIS pull.

## Next action

Open the draft statement in Storybook, then collect or credit. Amounts stay exact-decimal strings. Do not add a production SPA, PDF, or email in this slice.
