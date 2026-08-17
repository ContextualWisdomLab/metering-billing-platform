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
9. Export a journal proposal through `metering_billing.AccountingExportService` from a stored invoice draft, or a cash journal from a stored payment receipt.  Hand the proposal to AIS.  Do not post journals, open fiscal periods, or resolve statutory account IDs.
10. Open a collection case through `metering_billing.CollectionCaseService` from a stored invoice draft.  Send a dunning notice next.  Do not capture payment or post journals from that path.
11. Project a payment intent through `metering_billing.PaymentIntentService` from a stored collection case.  Bind a provider projection later or cancel the intent.  Do not capture, settle, or post from that path.
12. Record a payment receipt through `metering_billing.PaymentSettlementService` from a stored projected payment intent.  Apply the exact received amount, reduce collection outstanding, and settle the case when remaining is zero.  Do not capture via a provider or post journals.
13. Propose a cash journal through `metering_billing.AccountingExportService.propose_cash_journal` from a stored payment receipt.  Hand the persisted cash proposal to AIS.  Do not change collection outstanding, capture via a provider, or post journals.
