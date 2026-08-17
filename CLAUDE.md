# Metering Billing Platform Development Context

This repository is the CWL commercial usage and billing authority. It is provider-neutral and accounting-aware, but it is not the statutory accounting authority.

Before changing behavior:

1. Identify the authoritative fact being changed.
2. Add a failing test for monetary and idempotency behavior.
3. Preserve immutable history through correction or reversal records.
4. Keep provider integration behind capability-specific ports.
5. Export accounting proposals without claiming legal posting.
6. Ingest usage through `metering_billing.UsageIngestionService` so retries are idempotent and tenants cannot attribute usage to each other.
7. Rate stored usage through `metering_billing.UsageRatingService` so a tenant window produces exact invoice-intent totals from billable quality only.
8. Draft invoice intent through `metering_billing.InvoiceDraftService` from a stored rating run.  Do not issue, collect, or post from that path.
