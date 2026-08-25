# ADR 0011: HTTP Accept Surface Over Existing Commercial Services

**Status:** Accepted

## Context

The commercial path already exists in-process: usage ingest, windowed rating, invoice draft, journal proposal, collection case, payment intent, payment receipt, and cash journal proposal.  Buyers and the Accounting Information Platform cannot import those Python objects.  AIS is adding HTTP that returns `posting_receipt`.  Billing needs a matching accept surface that returns the published commercial contracts.

This repository is not the statutory accounting authority.  HTTP must not post a journal, store a card PAN, or bind Stripe, Adyen, or Toss.

## Decision

- Expose a stdlib WSGI app from `metering_billing.http_app.create_http_app`.  Do not add FastAPI, Flask, or Starlette.
- Keep existing service objects as the source of truth.  HTTP parses JSON, requires `tenant_reference` on every write, and returns each `as_contract_dict` result.
- Serve standalone as `python -m metering_billing.http_app` on `0.0.0.0:$PORT`.
- Map `accepted` and `duplicate_replay` to HTTP 200.  Map `rejected` and unreadable requests to HTTP 422.  Use HTTP 404 only for an unknown route.
- Keep money as exact-decimal strings.  Do not mask operational billing identifiers.
- Do not post journals, open fiscal periods, resolve statutory account IDs, or change AIS contracts.

## Consequences

- Buyers and AIS can call the already-built commercial path without an in-process Python dependency.
- Replay stays idempotent because the services, not the adapter, own identity.
- A later persistent ledger can replace `MemoryUsageLedger` without changing routes or status mapping.
