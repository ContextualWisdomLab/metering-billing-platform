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
- Export a journal proposal through `metering_billing.AccountingExportService` so a tenant and `invoice_draft_id` replay the same `proposal_id`.  Propose a cash journal from a stored `payment_receipt_id` so the same tenant, receipt, hash, and contract version replay the same `proposal_id`.  Proposals stay proposal-only; this repository does not post journals.
- Open a collection case through `metering_billing.CollectionCaseService` so a tenant and `invoice_draft_id` replay the same `collection_case_id`.  Dunning events are commercial reminders; they do not capture payment or post journals.
- Project a payment intent through `metering_billing.PaymentIntentService` so a tenant and `collection_case_id` replay the same `payment_intent_id`.  Intents stay projected; they do not capture, settle, or post.
- Record a payment receipt through `metering_billing.PaymentSettlementService` so a tenant, `payment_intent_id`, received amount, source-payload hash, and contract version replay the same `payment_receipt_id`.  Receipts stay `applied`; they do not capture via a provider or post journals.  Cancel a projected intent without writing a receipt or changing outstanding.
- Accept buyer and AIS writes through `metering_billing.http_app.create_http_app` or `python -m metering_billing.http_app`.  HTTP is a thin JSON adapter over the existing services.  Require a tenant on every write.  Money stays exact-decimal strings.  HTTP 200 means `accepted` or `duplicate_replay`; HTTP 422 means `rejected`; HTTP 404 is only an unknown route.  Do not post journals from that path.
- Let AIS pull persisted proposals through `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.  Require a tenant on every read.  Accept optional `X-CWL-Tenant-Reference`; body or query `tenant_reference` still works when the header is absent; a mismatch is 422.  Cash and AR proposals share `journal_proposal` and appear in the same list.  Query never mutates `proposal_status` and never emits `posted` or statutory account IDs.
- Pull an AIS posting receipt through `metering_billing.PostingReceiptPullService.pull_posting_receipt` and store one append-only `posting_receipt_observation`.  Validate the AIS-owned consumed contract.  Do not map `posting_status_code` onto `proposal_status`.  AIS 403 is cross-tenant and writes zero rows; AIS 404 is `not_yet_accepted` and writes zero rows.  `GET /v1/posting-receipt-observations/{idempotency_key}` reads a stored observation and does not call AIS.
- Record a commercial credit through `metering_billing.CreditAdjustmentService.record_credit_adjustment` so a tenant, `invoice_draft_id`, exact `credit_amount`, `credit_reason_code`, source-payload hash, and contract version replay the same `credit_adjustment_id` and `proposal_id`.  Credits stay `recorded`.  If a collection case exists, reduce outstanding by the same inclusive amount and settle at zero.  When a tax assessment exists, split the credit proportionally (`credit_tax_amount = round_half_even(credit_amount * tax_amount / tax_inclusive_amount)`) and emit a three-line unwind that debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`.  Untaxed credits stay two-line.  Do not post, call AIS, or emit statutory IDs.  Record the credit; AIS pulls the validated three-line unwind.
- Publish a tenant-scoped rate card through `metering_billing.RateCardService.publish_rate_card` so a tenant, card name, canonical line hash, and contract version replay the same `rate_card_version`.  Versions are append-only.  Rate stored usage against that persisted version; do not invent a hidden default price.  A missing metric on the card fails closed.  Publish a rate card, then rate a window against that version.
- Publish a tenant-scoped tax rate through `metering_billing.TaxRateService.publish_tax_rate` so a tenant, closed `tax_code`, exact `tax_rate` in `[0, 1]`, and contract version replay the same `tax_rate_version`.  Assess a stored draft through `metering_billing.TaxAssessmentService.assess_tax`.  Round tax half-even to the documented ISO 4217 minor units.  Reject assess after a collection case is open.  Collection outstanding uses `tax_inclusive_amount` when an assessment exists.  A taxed journal proposal credits semantic `tax_payable`; AIS must map that role.  Credit remaining is inclusive.  A taxed credit unwinds `tax_payable` with the proportional split.  Publish a tax rate, assess the draft, then propose the journal and let AIS pull.
- Leave principal, account, and project identifiers usable for invoicing.  Purpose-limit access; do not mask operational billing identifiers.
