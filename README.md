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

- closed JSON Schema contracts for usage events, provider capabilities, usage-ingestion receipts, rating runs, invoice drafts, collection cases, and semantically validated accounting journal proposals;
- a normalized PostgreSQL 18 core plus usage-identity, rating-run, invoice-draft, journal-proposal, and collection-case migrations with tenant-scoped attribution constraints;
- an importable `metering_billing` package that ingests immutable usage, rates tenant-scoped half-open windows, drafts invoice intent, emits proposal-only journals, and opens commercial collection cases;
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

## Rate a usage window

```bash
python3 -c "from metering_billing import TimeWindow, UsageRatingService"
```

Register a versioned rate card and exact unit prices on the same ledger, then call `UsageRatingService.rate_usage_window` with a tenant, a half-open ISO 8601 window, and a rate-card version. Only `meter_quality_rule` billable quality enters the invoice-intent total. An identical replay returns the same `rating_run_id` and exact totals. Rating does not draft an invoice, call a payment provider, or post a journal.

## Draft an invoice

```bash
python3 -c "from metering_billing import InvoiceDraftService"
```

After a `rating_run` exists, call `InvoiceDraftService.draft_invoice` with the tenant and `rating_run_id`. The draft total equals the rating-run billable total. An identical replay returns the same `invoice_draft_id`. Status stays `draft`. The draft does not issue, collect, call a payment provider, or post a journal.

## Propose a journal

```bash
python3 -c "from metering_billing import AccountingExportService"
```

After an `invoice_draft` exists, call `AccountingExportService.propose_journal` with the tenant and `invoice_draft_id`. The proposal is one balanced exact-decimal `accounting_journal_proposal` that uses semantic account roles and an intended book role. An identical replay returns the same `proposal_id`. Status stays inside the proposal lifecycle and is never `posted`.

## Open a collection case

```bash
python3 -c "from metering_billing import CollectionCaseService"
```

After an `invoice_draft` exists, call `CollectionCaseService.open_collection_case` with the tenant and `invoice_draft_id`. Outstanding equals the exact draft total. An identical replay returns the same `collection_case_id`. Status stays `open` or `dunning`. Then call `record_dunning_event` with `first_notice` or `overdue_notice`. Reminders do not capture money or post journals.

## Next action

Open the collection case from the persisted invoice draft, then send a dunning notice. Do not add a payment-provider adapter or payment intent until the case is the commercial source of collection. Do not mark the case paid, written off, or posted.
