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

- closed JSON Schema contracts for usage events, provider capabilities, usage-ingestion receipts, rating runs, invoice drafts, collection cases, payment intents, payment receipts, credit adjustments, rate cards, tax rates, tax assessments, and semantically validated accounting journal proposals, plus a consumed AIS posting-receipt contract;
- a normalized PostgreSQL 18 core plus usage-identity, rating-run, invoice-draft, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, rate-card-catalog, tax-assessment, and credit-tax-unwind migrations with tenant-scoped attribution constraints;
- an importable `metering_billing` package that ingests immutable usage, publishes versioned rate cards, rates tenant-scoped half-open windows against a persisted version, drafts invoice intent, publishes tax rates, assesses tax on a draft, emits proposal-only journals, opens commercial collection cases, projects provider-neutral payment intents, applies commercial payment receipts, records commercial credits, pulls AIS posting receipts as observations, and accepts those writes over a stdlib HTTP adapter;
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
```

Register the tenant, billing account, principal, meter, and quality rules on a `MemoryUsageLedger`, then call `UsageIngestionService.ingest_usage_batch`. Identical retries return `duplicate_replay` and leave the stored usage set unchanged. A changed hash or contract version for the same source key is rejected.

## Publish a rate card

```bash
python3 -c "from metering_billing import RateCardService"
```

Call `RateCardService.publish_rate_card` with a tenant, a two-or-more-word `snake_case` card name, an ISO currency, and one or more flat metric lines. Each `unit_amount` is an exact decimal greater than zero. The same tenant, name, canonical line hash, and contract version return `duplicate_replay`. A later distinct line set increments the version. A published version is never edited.

## Rate a usage window

```bash
python3 -c "from metering_billing import TimeWindow, UsageRatingService"
```

Publish a rate card, then call `UsageRatingService.rate_usage_window` with a tenant, a half-open ISO 8601 window, and that persisted rate-card version. Only `meter_quality_rule` billable quality enters the invoice-intent total. Rating uses the stored `unit_amount` for the matching `metric_code`. An unknown version, a cross-tenant version, or a missing metric fails closed. An identical replay returns the same `rating_run_id` and exact totals. Rating does not draft an invoice, call a payment provider, or post a journal.

## Draft an invoice

```bash
python3 -c "from metering_billing import InvoiceDraftService"
```

After a `rating_run` exists, call `InvoiceDraftService.draft_invoice` with the tenant and `rating_run_id`. The draft total equals the rating-run billable total. An identical replay returns the same `invoice_draft_id`. Status stays `draft`. The draft does not issue, collect, call a payment provider, or post a journal.

After an `invoice_draft` exists, call `InvoicePresentmentService.present_invoice_draft` or `GET /v1/invoice-drafts/{invoice_draft_id}` with the tenant. The statement shows exclusive, tax, inclusive, credited, and amount due as exact-decimal strings. Tax is zero when no assessment exists. Amount due is inclusive minus accepted credits and never below zero. `GET /v1/invoice-drafts` lists summaries. Open the draft statement, then collect or credit. The read does not post, call AIS, or start a web UI.

## Propose a journal

```bash
python3 -c "from metering_billing import AccountingExportService"
```

After an `invoice_draft` exists, call `AccountingExportService.propose_journal` with the tenant and `invoice_draft_id`. After a `payment_receipt` exists, call `AccountingExportService.propose_cash_journal` with the tenant and `payment_receipt_id`. Each proposal is one balanced exact-decimal `accounting_journal_proposal` that uses semantic account roles and an intended book role. An identical replay returns the same `proposal_id`. Status stays inside the proposal lifecycle and is never `posted`. Cash lines debit `cash_receipt` and credit `accounts_receivable`.

## Open a collection case

```bash
python3 -c "from metering_billing import CollectionCaseService"
```

After an `invoice_draft` exists, call `CollectionCaseService.open_collection_case` with the tenant and `invoice_draft_id`. Outstanding equals the exact draft total. An identical replay returns the same `collection_case_id`. Status stays `open` or `dunning`. Then call `record_dunning_event` with `first_notice` or `overdue_notice`. Reminders do not capture money or post journals.

## Project a payment intent

```bash
python3 -c "from metering_billing import PaymentIntentService"
```

After a `collection_case` exists, call `PaymentIntentService.project_payment_intent` with the tenant and `collection_case_id`. The intent amount equals the exact case outstanding. An identical replay returns the same `payment_intent_id`. Status stays `projected`. The intent does not capture, settle, store a card PAN, or post a journal.

## Record a payment receipt

```bash
python3 -c "from metering_billing import PaymentSettlementService"
```

After a projected `payment_intent` exists, call `PaymentSettlementService.record_payment_receipt` with the tenant, `payment_intent_id`, and exact `received_amount`. The receipt status is `applied`. The linked collection-case outstanding is reduced by the same amount; remaining zero marks the case `settled`. An identical replay returns the same `payment_receipt_id`. Call `cancel_payment_intent` to flip a projected intent to `cancelled` without writing a receipt. The receipt does not capture via a provider, emit an `accounting_journal_proposal`, or post a journal.

## Accept writes over HTTP

```bash
python3 -c "from metering_billing.http_app import create_http_app"
python3 -m metering_billing.http_app
```

`create_http_app(ledger=...)` is a thin stdlib WSGI adapter over the services above. Standalone serving binds `0.0.0.0:$PORT` (default 8000). Every write requires `tenant_reference`. Money stays exact-decimal strings. HTTP 200 means `accepted` or `duplicate_replay` on writes, or a successful read. HTTP 422 means `rejected` or an unreadable request. HTTP 404 is an unknown route or an unknown/cross-tenant proposal, credit, observation, rate-card version, or invoice draft. The adapter does not post journals or call a named payment provider.

## Present an invoice draft

```bash
python3 -c "from metering_billing import InvoicePresentmentService"
# GET /v1/invoice-drafts/{invoice_draft_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/invoice-drafts?tenant_reference=urn:cwl:tenant_001
```

After an `invoice_draft` exists, `GET /v1/invoice-drafts/{invoice_draft_id}` returns the tenant-scoped statement. Open the draft statement, then collect or credit.

## Pull journal proposals

```bash
# GET /v1/journal-proposals?tenant_reference=urn:cwl:tenant_001
# GET /v1/journal-proposals/{proposal_id}?tenant_reference=urn:cwl:tenant_001
```

AIS pulls validated proposals from the same stdlib app. Pin the tenant with optional `X-CWL-Tenant-Reference` or with `tenant_reference` in the query or JSON body. If both are present they must match. Cash, AR, and credit proposals share `journal_proposal` and appear in the same list. Query never marks a proposal exported, posted, or consumed. Billing emits semantic account roles only; AIS maps `cash_receipt` to its own chart.

## Pull a posting receipt

```bash
python3 -c "from metering_billing import PostingReceiptPullService"
# POST /v1/posting-receipt-observations
# GET /v1/posting-receipt-observations/{idempotency_key}
```

After AIS accepts a validated proposal, an operator pulls the AIS `posting_receipt` and Billing stores it as a commercial observation. If AIS returns 404, accept the proposal on AIS and retry. `posting_status_code` stays an AIS fact. Billing `proposal_status` stays `validated`. GET reads a previously stored observation and does not call AIS.

## Record a credit adjustment

```bash
python3 -c "from metering_billing import CreditAdjustmentService"
# POST /v1/credit-adjustments
# GET /v1/credit-adjustments/{credit_adjustment_id}
```

After an `invoice_draft` exists, call `CreditAdjustmentService.record_credit_adjustment` with the tenant, `invoice_draft_id`, exact `credit_amount`, and a closed `credit_reason_code` (`rating_correction`, `goodwill`, or `billing_error`). The credit cannot exceed remaining adjustable consideration. If a collection case exists, outstanding is reduced by the same inclusive amount; remaining zero marks the case `settled`. When a tax assessment exists, the credit is split proportionally and the journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`. Untaxed credits stay two-line. An identical replay returns the same `credit_adjustment_id` and `proposal_id`. Record the credit; AIS pulls the validated three-line unwind. This path does not post, call AIS, refund-to-card, or chargeback.

## Publish a tax rate and assess a draft

```bash
python3 -c "from metering_billing import TaxRateService, TaxAssessmentService"
```

Call `TaxRateService.publish_tax_rate` with a tenant, a closed `tax_code` (`vat`, `gst`, or `sales_tax`), and an exact `tax_rate` in `[0, 1]`. Then call `TaxAssessmentService.assess_tax` with the tenant, `invoice_draft_id`, and that persisted version. Tax is half-even rounded to the documented ISO 4217 minor units. Assess before opening a collection case. A taxed `propose_journal` credits semantic `tax_payable`; AIS must map that role.

## Next action

Open the draft statement, then collect or credit. Do not start a web UI, PDF, or email in this slice.
