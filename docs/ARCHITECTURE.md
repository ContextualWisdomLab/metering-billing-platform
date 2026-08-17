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
- `invoice_management`: invoice intent and explainable lines.
- `provider_gateway`: capability discovery, mapping, commands, and webhooks.
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

`metering_billing.UsageRatingService` is the rating authority for already-stored usage.  A buyer supplies a tenant, a half-open ISO 8601 window, and a rate-card version.  The service hashes the tenant's usage snapshot in that window, rates only `billable` quality, and persists append-only `rating_run` and `rating_line` rows with exact decimal money.  A replay of the same identity returns the stored `rating_run_id` and totals.  Rating does not draft an invoice, call a payment provider, or emit a posted journal.

## Failure policy

- Duplicate input returns the existing receipt.
- A source-event key replay with a different payload hash or contract version is a conflict.
- Attribution URNs that leave the event tenant are rejected.
- Invalid quality or meter configuration fails closed.
- Provider timeouts retain internal facts and retry idempotently.
- Existing provider objects never fail over automatically to a different provider.
- Accounting rejection does not rewrite billing facts; it creates a reconciliation exception.
