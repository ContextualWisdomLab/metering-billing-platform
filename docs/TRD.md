# Technical Requirements Document

## Architecture style

Start as a modular, contract-first repository. Runtime services can later be deployed as a modular monolith and split at stable ports when throughput or PCI/network boundaries justify separation.

## Contract plane

- JSON Schema Draft 2020-12 for external payloads.
- Exact decimal values represented as strings at API boundaries.
- UUIDv7 identifiers for new records where PostgreSQL 18 generates IDs.
- Idempotency keys on all state-changing external commands.
- Usage ingestion deduplicates on tenant-scoped `source_event_key` and on `source_payload_hash` plus `event_contract_version`.
- CloudEvents-compatible envelopes in the event milestone.

## Usage-ingestion plane

The importable `metering_billing` package is the first runtime module.  It can run in-process against the in-memory third-normal-form ledger that mirrors the PostgreSQL constraints.  A later adapter can persist the same rows without changing the hash, tenant, decimal, or rating-identity rules.  Canonical source-payload hashing excludes envelope identifiers (`event_id`, `source_event_key`), `source_payload_hash`, and `recorded_at`.  Batch ingest, usage queries, and rating accept half-open ISO 8601 windows.  Rating identity is tenant, window, persisted rate-card version, and usage-snapshot hash.

## Persistence plane

PostgreSQL is the authoritative store for normalized records. Raw webhook and usage payloads will be stored immutably in S3-compatible object storage in a later milestone; relational records retain hashes and references.

## Provider plane

Provider integration is capability-based. Checkout, subscription, usage export, invoice export, collection, refund, dispute, tax document, and settlement are separate ports. A provider implements only declared capabilities.

## Accounting plane

`metering_billing.AccountingExportService` produces semantically validated, balanced journal proposals from a persisted invoice draft or payment receipt using semantic account roles and an intended book role. Cash proposals debit `cash_receipt` and credit `accounts_receivable`. Credit proposals debit `usage_revenue` and credit `accounts_receivable`. The Accounting Information Platform resolves authoritative chart-account IDs, accounting policy, legal entity, accounting book, fiscal period, currency treatment, revenue recognition, and final posting. Billing never claims that posting.

`metering_billing.CollectionCaseService` opens commercial collection cases from those drafts and appends dunning reminders. Collection does not capture payment or post a journal.

`metering_billing.PaymentIntentService` projects a provider-neutral payment intent from a stored collection case. The intent does not capture, settle, store a card PAN, or post a journal.

`metering_billing.PaymentSettlementService` applies a commercial payment receipt against a projected intent and reduces collection outstanding. Receipts stay `applied` and do not capture via a provider or post a journal. AIS consumes the later cash journal proposal, not the receipt itself.

`metering_billing.http_app.create_http_app` exposes those services as stdlib JSON HTTP. The adapter requires a tenant on every write, returns published `as_contract_dict` contracts, and never posts a journal.

`GET /v1/journal-proposals` is a safe collection read (Fielding et al., 2022; Google, 2024). AIS pulls validated proposals and may pin `X-CWL-Tenant-Reference`. Body or query `tenant_reference` still works when the header is absent; a mismatch is HTTP 422. Cash, AR, and credit rows share `journal_proposal`. Query does not flip `proposal_status`; AIS later returns `posting_receipt`. Billing emits semantic account roles only.

`metering_billing.PostingReceiptPullService` GETs that AIS receipt with stdlib `urllib` and stores a commercial observation. The consumed schema stays AIS-owned. `POST /v1/posting-receipt-observations` is the operator trigger; `GET /v1/posting-receipt-observations/{idempotency_key}` is a safe stored read and does not call AIS (Fielding et al., 2022). `posting_status_code` is not mapped onto `proposal_status`. If AIS returns 404, the operator accepts the proposal on AIS and retries.

`metering_billing.CreditAdjustmentService` records a commercial credit against a stored invoice draft and emits one validated journal proposal. IFRS 15 treats the credit as variable consideration, not as proof that revenue has been reversed in the statutory books (IFRS Foundation, 2024). ISO 20022 keeps the commercial credit note separate from a posted `camt` settlement (International Organization for Standardization, 2026). `POST /v1/credit-adjustments` records the credit; AIS later pulls the proposal. `GET /v1/credit-adjustments/{credit_adjustment_id}` is a safe stored read and does not call AIS (Fielding et al., 2022).

`metering_billing.RateCardService` publishes a tenant-scoped catalog version with flat unit prices (TM Forum, 2024). Replay of the same tenant, card name, canonical line hash, and contract version returns the stored `rate_card_version` (Helland, 2012). `UsageRatingService` must resolve that persisted version; a missing metric fails closed and does not invent a price. `POST /v1/rate-cards` publishes a version. `GET /v1/rate-cards`, `GET /v1/rate-cards/{rate_card_id}`, `GET /v1/rate-cards/{rate_card_id}/versions`, and `GET /v1/rate-card-versions/{rate_card_version}` are safe tenant-scoped reads (Fielding et al., 2022). Publish a rate card, then rate a window against that version.

`metering_billing.TaxRateService` publishes a tenant-scoped tax-rate version. `metering_billing.TaxAssessmentService` applies that version to a stored invoice draft and half-even rounds to ISO 4217 minor units (International Organization for Standardization, 2015; OECD, 2017). IFRS 15 keeps amounts collected for a tax authority out of revenue (IFRS Foundation, 2024). A taxed journal proposal credits semantic `tax_payable`; AIS must map that role and owns IAS 12 presentation (IFRS Foundation, n.d.). `POST /v1/tax-rates` and `POST /v1/tax-assessments` write; GET list, version, and assessment routes are safe tenant-scoped reads (Fielding et al., 2022). Publish a tax rate, assess the draft, then propose the journal and let AIS pull.

## Security

- No card number, CVC, provider secret, PAT plaintext, prompt, or response is accepted in billing contracts.
- Webhooks are evidence until signature verification and normalization succeed.
- Tenant isolation is enforced in the application and database layers.
- Historical facts are corrected by compensating records.
