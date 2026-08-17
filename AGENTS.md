# Agent Development Rules

## Authority

- Preserve the billing-versus-accounting boundary.
- Never use a provider object ID as an internal primary key.
- Never let a webhook directly grant entitlement or post accounting.

## Data

- Use two-or-more-word `snake_case` database identifiers.
- Keep normalized facts relational; raw provider payloads belong in immutable object storage.
- Never store card data, PAT plaintext, prompt text, response text, or provider secrets.
- Use exact decimals for money and billable quantities.

## Development

- Write a failing test before behavior code.
- Require production statement and branch coverage of 100%.
- Document every public API and every accounting or monetary invariant.
- Update architecture, ADRs, and CHANGELOG when authority or behavior changes.
- Keep usage ingestion append-only.  Deduplicate by tenant-scoped source-event key and by source-payload hash plus contract version.
- Rate stored usage through `metering_billing.UsageRatingService` so a tenant window, rate-card version, and usage snapshot replay the same `rating_run_id` and exact totals.
- Draft invoice intent through `metering_billing.InvoiceDraftService` so a tenant and `rating_run_id` replay the same `invoice_draft_id` and exact totals.  Drafts are not issued, collected, or posted.
- Export a journal proposal through `metering_billing.AccountingExportService` so a tenant and `invoice_draft_id` replay the same `proposal_id`.  Proposals stay proposal-only; this repository does not post journals.
- Open a collection case through `metering_billing.CollectionCaseService` so a tenant and `invoice_draft_id` replay the same `collection_case_id`.  Dunning events are commercial reminders; they do not capture payment or post journals.
- Project a payment intent through `metering_billing.PaymentIntentService` so a tenant and `collection_case_id` replay the same `payment_intent_id`.  Intents stay projected; they do not capture, settle, or post.
- Leave principal, account, and project identifiers usable for invoicing.  Purpose-limit access; do not mask operational billing identifiers.
