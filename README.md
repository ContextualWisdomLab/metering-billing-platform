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

The first milestone contains:

- closed JSON Schema contracts for usage events, provider capabilities, usage-ingestion receipts, and semantically validated accounting journal proposals;
- a normalized PostgreSQL 18 core migration with tenant-scoped attribution constraints;
- an importable `metering_billing` package that ingests immutable usage and deduplicates by source-event key plus source-payload hash and contract version;
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

## Next action

After usage ingestion merges, implement deterministic meter aggregation and commercial rating before adding a payment-provider adapter. Do not post accounting journals from billing.
