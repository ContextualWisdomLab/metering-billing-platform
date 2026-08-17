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

`metering_billing.http_app.create_http_app` exposes those services as stdlib JSON HTTP. The adapter requires a tenant on every write, returns published `as_contract_dict` contracts, and never posts a journal. After a tenant has one or more active API credentials, every `/v1` call except credential issue also requires a matching bearer or `X-CWL-Api-Key` secret (National Institute of Standards and Technology, 2020; OWASP, 2023; American Institute of Certified Public Accountants, 2017).

`GET /v1/journal-proposals` is a safe collection read (Fielding et al., 2022; Google, 2024). AIS pulls validated proposals and may pin `X-CWL-Tenant-Reference`. Body or query `tenant_reference` still works when the header is absent; a mismatch is HTTP 422. That pull keeps working until a key is issued for the tenant. Cash, AR, and credit rows share `journal_proposal`. Query does not flip `proposal_status`; AIS later returns `posting_receipt`. Billing emits semantic account roles only.

`metering_billing.PostingReceiptPullService` GETs that AIS receipt with stdlib `urllib` and stores a commercial observation. The consumed schema stays AIS-owned. `POST /v1/posting-receipt-observations` is the operator trigger; `GET /v1/posting-receipt-observations/{idempotency_key}` is a safe stored read and does not call AIS (Fielding et al., 2022). `posting_status_code` is not mapped onto `proposal_status`. If AIS returns 404, the operator accepts the proposal on AIS and retries.

`metering_billing.AisOutboxDrainService` consumes AIS `GET /outbox-events?event_type_code=posting_receipt` and then the same receipt GET (Fielding et al., 2022). Matching is equality against URNs constructed from our stored `proposal_id`; Billing does not parse `payload_reference`. Empty unpublished pages skip receipt GETs. After a stored observation, `POST /outbox-events/{outbox_event_id}/publish` is idempotent. `POST /v1/ais-outbox-drains` is the explicit drain. Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.

`metering_billing.CollectionCasePresentmentService` projects a stored collection case into a commercial statement. IFRS 15 treats remaining outstanding as presentation of consideration, not collected revenue (IFRS Foundation, 2024). `GET /v1/collection-cases/{collection_case_id}` and `GET /v1/collection-cases` are safe tenant-scoped reads (Fielding et al., 2022; Google, 2024). `operator_console` Storybook presents outstanding and the next action. Open the collection case, then collect or credit.

`metering_billing.CreditAdjustmentService` records a commercial credit against a stored invoice draft and emits one validated journal proposal. IFRS 15 treats the credit as variable consideration, not as proof that revenue has been reversed in the statutory books (IFRS Foundation, 2024). When a tax assessment exists the inclusive credit is split proportionally and the journal debits semantic `tax_payable` as well as `usage_revenue` (IFRS Foundation, n.d.; International Organization for Standardization, 2015). ISO 20022 keeps the commercial credit note separate from a posted `camt` settlement (International Organization for Standardization, 2026). `POST /v1/credit-adjustments` records the credit; AIS later pulls the proposal. `GET /v1/credit-adjustments/{credit_adjustment_id}` is a safe stored read and does not call AIS (Fielding et al., 2022). Record the credit; AIS pulls the validated three-line unwind.

`metering_billing.RateCardService` publishes a tenant-scoped catalog version with flat unit prices (TM Forum, 2024). Replay of the same tenant, card name, canonical line hash, and contract version returns the stored `rate_card_version` (Helland, 2012). `UsageRatingService` must resolve that persisted version; a missing metric fails closed and does not invent a price. `POST /v1/rate-cards` publishes a version. `GET /v1/rate-cards`, `GET /v1/rate-cards/{rate_card_id}`, `GET /v1/rate-cards/{rate_card_id}/versions`, and `GET /v1/rate-card-versions/{rate_card_version}` are safe tenant-scoped reads (Fielding et al., 2022). Publish a rate card, then rate a window against that version.

`metering_billing.TaxRateService` publishes a tenant-scoped tax-rate version. `metering_billing.TaxAssessmentService` applies that version to a stored invoice draft and half-even rounds to ISO 4217 minor units (International Organization for Standardization, 2015; OECD, 2017). IFRS 15 keeps amounts collected for a tax authority out of revenue (IFRS Foundation, 2024). A taxed journal proposal credits semantic `tax_payable`; AIS must map that role and owns IAS 12 presentation (IFRS Foundation, n.d.). `POST /v1/tax-rates` and `POST /v1/tax-assessments` write; GET list, version, and assessment routes are safe tenant-scoped reads (Fielding et al., 2022). Publish a tax rate, assess the draft, then propose the journal and let AIS pull.

`metering_billing.InvoicePresentmentService` projects a stored invoice draft into a commercial statement. IFRS 15 treats that statement as presentation of consideration, not earned revenue (IFRS Foundation, 2024). ISO 20022 keeps the commercial invoice document separate from a posted financial message (International Organization for Standardization, 2026). `GET /v1/invoice-drafts/{invoice_draft_id}` and `GET /v1/invoice-drafts` are safe tenant-scoped reads (Fielding et al., 2022; Google, 2024). `operator_console` Storybook presents the same exact-decimal JSON. Open the draft statement, then collect or credit.

`metering_billing.TenantApiCredentialService` issues append-only HTTP API credentials. NIST SP 800-63B requires the verifier to store a keyed hash, never the recoverable secret (National Institute of Standards and Technology, 2020). OWASP treats leaked keys as revocable bearer credentials (OWASP, 2023). SOC 2 CC6 requires logical access control on a shippable HTTP surface (American Institute of Certified Public Accountants, 2017). `POST /v1/tenant-api-credentials` returns the secret once. `GET /v1/tenant-api-credentials` is a safe metadata list and never includes the secret (Fielding et al., 2022). `GET /healthz` stays unauthenticated. Issue a key, then send it on every `/v1` call; revoke when leaked.

`metering_billing.WebhookSubscriptionService` registers an https callback and returns the signing secret once. The verifier stores a keyed HMAC, never the recoverable secret (Krawczyk et al., 1997; National Institute of Standards and Technology, 2020). `WebhookDeliveryService.deliver_due_events` POSTs the published commercial envelope and signs the raw body; receivers check `X-CWL-Webhook-Signature` rather than HTTP Message Signatures (Fielding et al., 2022; Backman et al., 2024). `GET /v1/webhook-subscriptions` is a safe metadata list and never includes the secret. Register an https callback, then run deliveries; AIS may keep polling.

## Security

- No card number, CVC, provider secret, PAT plaintext, prompt, response, tenant API credential plaintext, or webhook-subscription plaintext is accepted in billing contracts after issue or register. The issue and register responses are the only places those secrets appear.
- Webhooks are evidence until signature verification and normalization succeed.
- Tenant isolation is enforced in the application and database layers.
- Historical facts are corrected by compensating records.
