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

- closed JSON Schema contracts for usage events, usage-event presentment, provider capabilities, usage-ingestion receipts, rating runs, rating-run presentment, invoice drafts, collection cases, collection-case presentment, collection-aging presentment, account-statement presentment, rated-spend presentment, spend budgets, spend-budget presentment, payment intents, payment-intent presentment, payment receipts, payment-receipt presentment, unapplied cash, unapplied-cash presentment, unapplied-cash applications, unapplied-cash-application presentment, credit adjustments, credit-adjustment presentment, issued credit notes, issued-credit-note presentment, issued-credit-note voids, issued-credit-note-void presentment, credit-note applications, credit-note-application presentment, collection-case settlements, collection-case-settlement presentment, rate cards, rate-card presentment, tax rates, tax assessments, tax-assessment presentment, posting-receipt observation presentment, tenant API credentials, webhook subscriptions, webhook deliveries, AIS outbox drains, and semantically validated accounting journal proposals, plus a consumed AIS posting-receipt contract;
- a normalized PostgreSQL 18 core plus usage-identity, rating-run, invoice-draft, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, spend-budget, rate-card-catalog, tax-assessment, credit-tax-unwind, tenant-api-credential, webhook-outbox, issued-invoice, and issued-credit-note migrations with tenant-scoped attribution constraints;
- a durable PostgreSQL usage-to-issued-invoice, collection, payment, credit, cash-journal, and webhook-delivery vertical: rate-card versions, rating runs, invoice drafts, issued-invoice lines, tenant-scoped tax rates and assessments, collection cases and dunning events, payment intents, applied payment receipts with row-locked settlement, collection write-offs with exact-zero outstanding, explicit settle-when-zero facts, credit adjustments with collection reduction, balanced cash and credit journal proposals, tenant-scoped webhook subscriptions, delivery attempts, delivered status, and atomic commercial outbox replay paths;
- an importable `metering_billing` package that ingests immutable usage, publishes versioned rate cards, rates tenant-scoped half-open windows against a persisted version, presents already-rated spend by product, publishes commercial spend budgets, drafts invoice intent, issues commercial invoices, issues commercial credit notes, voids unused issued credit notes, publishes tax rates, assesses tax on a draft, emits proposal-only journals, opens commercial collection cases, presents collection aging, projects provider-neutral payment intents, applies commercial payment receipts, records commercial credits, pulls AIS posting receipts as observations, drains AIS posting-receipt outbox events, issues tenant API credentials, registers webhook callbacks for accepted commercial facts, and accepts those writes over a stdlib HTTP adapter;
- an importable `operator_console` Storybook that renders the invoice-draft, issued-invoice, issued-credit-note, collection-case, collection-aging, account-statement, rated-spend, spend-budget, budget-status, payment-intent, payment-receipt, credit-adjustment, rate-card, usage-event, rating-run, tax-assessment, and posting-receipt-observation presentment contracts with design tokens and exact-decimal fixtures;
- explicit billing-versus-accounting boundaries;
- offline repository validation with 100% line and branch coverage;
- exact-head CI with commit-pinned actions.

## Run the platform in three commands

Start your own billing platform with Docker Compose.  The database starts,
schema migrations apply automatically before the API accepts its first
request, and the API reports readiness when the durable PostgreSQL backend
answers:

```bash
cp compose/.env.example compose/.env   # review the defaults; change them before production
docker compose -f compose/docker-compose.yml up -d --wait
open http://localhost:8000/readyz      # expect {"status": "ready", "backend": "postgres"}
```

When `--wait` returns, the API is serving on port 8000 and you can start
accepting usage writes over HTTP right away.  The measured performance of
this deployment is recorded in `docs/operations/load-test-baseline.md`
(ADR 0124); re-run `compose/k6/e2e_smoke.js` against a healthy stack to add
your own dated numbers.

## Run validation

```bash
uv sync --group dev
METERING_BILLING_POSTGRES_DSN='dbname=metering_billing_usage_repo_test' uv run --locked --group dev python scripts/migrate_postgres.py --dsn 'dbname=metering_billing_usage_repo_test'
uv run --locked --group dev python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --group dev python -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
uv run --locked --group dev python -m coverage report --fail-under=100 --show-missing
uv run --locked --group dev python scripts/validate_repository.py
```

The repository uses a project-local `.venv` managed by `uv`; the checked-in
`uv.lock` pins the development and PostgreSQL runtime dependencies.  The
integration suite expects PostgreSQL 18 at
`METERING_BILLING_POSTGRES_DSN` and uses only a dedicated test database.
The migration runner records SHA-256 checksums in
`public.metering_billing_schema_migration` and takes a transaction-scoped
advisory lock; changing an applied migration is rejected.

## Ingest usage

```bash
python3 -c "from metering_billing import PostgresUsageLedger, UsageIngestionService"
# POST /v1/usage-events
# GET /v1/usage-events/{usage_event_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/usage-events?tenant_reference=urn:cwl:tenant_001
```

Register the tenant, billing account, principal, meter, and quality rules on a
`PostgresUsageLedger` created with a migrated PostgreSQL connection, then call
`UsageIngestionService.ingest_usage_batch`. `MemoryUsageLedger` remains the
fast in-process reference option. Identical retries return
`duplicate_replay` and leave the stored usage set unchanged; concurrent
retries are arbitrated by PostgreSQL and leave one event plus one receipt per
attempt. A changed hash or contract version for the same source key is
rejected. `POST /v1/usage-events` stays that ingest and refuses PAN and
provider secrets. After a `usage_event` exists,
`GET /v1/usage-events/{usage_event_id}` returns the tenant-scoped statement.
`GET /v1/usage-events` lists `{usage_events, next_cursor}`. Ingest usage, then
rate a window against a published card. This durable slice does not claim that
the rest of the commercial services, backup/restore, or production readiness
controls are complete. The durable vertical now includes subscription metadata,
delivery attempts, and outbox delivered status, but the one-time webhook secret
remains process-local; issue #84 remains open for a secure secret provider and
the remaining commercial, recovery, HA, readiness, and production-default work.

## Publish a rate card

```bash
python3 -c "from metering_billing import RateCardService"
```

Call `RateCardService.publish_rate_card` with a tenant, a two-or-more-word `snake_case` card name, an ISO currency, and one or more flat metric lines. Each `unit_amount` is an exact decimal greater than zero. The same tenant, name, canonical line hash, and contract version return `duplicate_replay`. A later distinct line set increments the version. A published version is never edited.

## Rate a usage window

```bash
python3 -c "from metering_billing import TimeWindow, UsageRatingService"
# POST /v1/rating-runs
# GET /v1/rating-runs/{rating_run_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/rating-runs?tenant_reference=urn:cwl:tenant_001
```

Publish a rate card, then call `UsageRatingService.rate_usage_window` with a tenant, a half-open ISO 8601 window, and that persisted rate-card version. Only `meter_quality_rule` billable quality enters the invoice-intent total. Rating uses the stored `unit_amount` for the matching `metric_code`. An unknown version, a cross-tenant version, or a missing metric fails closed. An identical replay returns the same `rating_run_id` and exact totals. `POST /v1/rating-runs` stays that command and refuses PAN and provider secrets. After a `rating_run` exists, `GET /v1/rating-runs/{rating_run_id}` returns the tenant-scoped statement. `GET /v1/rating-runs` lists `{rating_runs, next_cursor}`. Rate a window, then draft an invoice. Rating does not draft an invoice, call a payment provider, or post a journal.

## Draft an invoice

```bash
python3 -c "from metering_billing import InvoiceDraftService"
```

After a `rating_run` exists, call `InvoiceDraftService.draft_invoice` with the tenant and `rating_run_id`. The draft total equals the rating-run billable total. An identical replay returns the same `invoice_draft_id`. Status stays `draft`. The draft does not issue, collect, call a payment provider, or post a journal.

After an `invoice_draft` exists, call `InvoicePresentmentService.present_invoice_draft` or `GET /v1/invoice-drafts/{invoice_draft_id}` with the tenant. The statement shows exclusive, tax, inclusive, credited, and amount due as exact-decimal strings. Tax is zero when no assessment exists. Amount due is inclusive minus accepted credits and never below zero. `GET /v1/invoice-drafts` lists summaries. Open the draft statement, then collect or credit. The read does not post or call AIS.

After an `invoice_draft` exists, call `IssuedInvoiceService.issue_invoice` or `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` with the tenant. Replay of the same tenant and draft returns the same `issued_invoice_id`. Totals freeze the draft or its tax assessment. First successful issue enqueues one existing `invoice.issued` webhook outbox event. `GET /v1/issued-invoices/{issued_invoice_id}` and `GET /v1/issued-invoices` present the snapshot. Item GET includes optional stored `tax_assessment_id` when the draft assessment still matches the frozen totals. Issue invoice, then collect or credit. An unused issue may later be voided through `IssuedInvoiceVoidService.void_issued_invoice`. The path does not invent statutory numbering, capture payment, or call AIS.

After a `credit_adjustment` exists, call `IssuedCreditNoteService.issue_credit_note` or `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` with the tenant. Replay of the same tenant and credit returns the same `issued_credit_note_id`. Totals freeze the stored exclusive, tax, and inclusive credit amounts. First successful issue enqueues one existing `credit_note.issued` webhook outbox event. `GET /v1/issued-credit-notes/{issued_credit_note_id}` and `GET /v1/issued-credit-notes` present the snapshot. Item GET includes optional stored `tax_assessment_id` when the draft assessment still reproduces the frozen credit split. Issue the credit note; the validated journal remains available for AIS. An unused issued credit note may later be voided through `IssuedCreditNoteVoidService.void_issued_credit_note`. The path does not invent statutory numbering, capture payment, or call AIS.

After an `issued_credit_note` and an open `collection_case` exist for the same invoice, call `CreditNoteApplicationService.apply_credit_note` or `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` with the tenant and `issued_credit_note_id`. Replay of the same tenant and issued credit note returns the same `credit_note_application_id` and never double-reduces outstanding. First successful apply enqueues one existing `credit_note.applied` webhook outbox event. `GET /v1/credit-note-applications/{credit_note_application_id}` and `GET /v1/credit-note-applications` present the stored application. Apply the issued credit note, then collect the residual. The path does not invent a journal, tax unwind, second webhook system, statutory numbering, write-off, settlement, capture payment, or call AIS.

After an open `collection_case` already shows exact-zero outstanding, call `CollectionCaseSettlementService.settle_collection_case` or `POST /v1/collection-cases/{collection_case_id}/settlements`. Replay of the same tenant and case returns the same `collection_case_settlement_id` and never double-settles. First successful settle enqueues one existing `collection.settled` webhook outbox event. `GET /v1/collection-case-settlements/{collection_case_settlement_id}` and `GET /v1/collection-case-settlements` present the stored settlement. Settle the zero-outstanding case, then wait. The path does not invent a journal, tax unwind, second webhook system, statutory numbering, write-off, payment receipt, or AIS call.

After an open or dunning `collection_case` is disputed, call `CollectionDisputeService.hold_collection_case` or `POST /v1/collection-cases/{collection_case_id}/disputes`. Replay of the same tenant and case returns the same `collection_dispute_id` and never changes remaining outstanding. Case status becomes `disputed`. New dunning, payment receipt, credit apply, leftover apply, write-off, settle-when-zero, and void fail closed while held. First successful hold enqueues one existing `dispute.held` webhook outbox event. `GET /v1/collection-disputes/{collection_dispute_id}` and `GET /v1/collection-disputes` present the stored hold. Hold the disputed case, then wait. After a held `collection_dispute` exists, call `CollectionDisputeReleaseService.release_collection_dispute` or `POST /v1/collection-disputes/{collection_dispute_id}/releases`. Replay of the same tenant and dispute returns the same `collection_dispute_id` and never changes remaining outstanding. Dispute status becomes `released`. Case status returns to `open` or `dunning`. After release, dunning and money-close commands follow the existing open-case rules. First successful release enqueues one existing `dispute.released` webhook outbox event. `GET /v1/collection-dispute-releases/{collection_dispute_id}` and `GET /v1/collection-dispute-releases` present the stored release. Release the hold, then collect or dunn. The path does not invent a journal, second webhook system, write-off rewrite, void rewrite, statutory numbering, or AIS call.

After an open `collection_case` still shows leftover remaining, call `CollectionWriteOffService.write_off_collection_case` or `POST /v1/collection-cases/{collection_case_id}/write-offs`. Replay of the same tenant and case returns the same `collection_write_off_id` and never re-zeros outstanding. Remaining becomes exact zero without settling. First successful write-off enqueues one existing `write_off.recorded` webhook outbox event. `GET /v1/collection-write-offs/{collection_write_off_id}` and `GET /v1/collection-write-offs` present the stored write-off. After the write-off exists, `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals` composes one validated write-off/AR journal for AIS to pull. Write off leftover remaining, compose the journal, then settle. The path does not invent a tax unwind, second webhook system, statutory numbering, payment receipt, credit note, settlement command, or AIS call.

## Propose a journal

```bash
python3 -c "from metering_billing import AccountingExportService"
```

After an `invoice_draft` exists, call `AccountingExportService.propose_journal` with the tenant and `invoice_draft_id`. After a `payment_receipt` exists, the receipt write already proposed the cash journal; `AccountingExportService.propose_cash_journal` remains a manual replay. After a `credit_adjustment` exists, credit accept already proposed the credit journal; `AccountingExportService.propose_credit_journal` or `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals` remains an explicit compose or replay. After a `collection_write_off` exists, call `AccountingExportService.propose_write_off_journal` or `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals`. After an `unapplied_cash_refund` exists, call `AccountingExportService.propose_refund_journal` or `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals`. After parked `unapplied_cash` exists, call `AccountingExportService.propose_unapplied_cash_journal` or `POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals`. After an `unapplied_cash_application` exists, call `AccountingExportService.propose_unapplied_cash_application_journal` or `POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals`. After an `issued_invoice_void` exists, call `AccountingExportService.propose_void_journal` or `POST /v1/issued-invoice-voids/{issued_invoice_void_id}/journal-proposals`. After an unused `issued_credit_note_void` exists, call `AccountingExportService.propose_credit_note_void_journal` or `POST /v1/issued-credit-note-voids/{issued_credit_note_void_id}/journal-proposals`. Each proposal is one balanced exact-decimal `accounting_journal_proposal` that uses semantic account roles and an intended book role. An identical replay returns the same `proposal_id`. Status stays inside the proposal lifecycle and is never `posted`. Cash lines debit `cash_receipt` and credit `accounts_receivable`. Credit lines debit `usage_revenue` and credit `accounts_receivable`. Write-off lines debit `write_off_expense` and credit `accounts_receivable`. Refund lines debit `unapplied_cash` and credit `cash_receipt`. Leftover-park lines debit `cash_receipt` and credit `unapplied_cash`. Leftover-apply lines debit `unapplied_cash` and credit `accounts_receivable`. Void lines debit `usage_revenue` and credit `accounts_receivable`; taxed unused issues also debit `tax_payable`. Credit-note void lines debit `accounts_receivable` and credit `usage_revenue`; taxed unused notes also credit `tax_payable`. The void proposal binds the original invoice journal by Billing `proposal_id` only and never emits `journal_entry_id`. The credit-note void proposal binds the original credit journal by Billing `proposal_id` plus `credit_adjustment_id` / `issued_credit_note_id` only and fails closed if that original is missing.

## Open a collection case

```bash
python3 -c "from metering_billing import CollectionCaseService"
```

After an `invoice_draft` exists, call `CollectionCaseService.open_collection_case` with the tenant and `invoice_draft_id`. Outstanding equals the exact draft total. An identical replay returns the same `collection_case_id`. Status stays `open` or `dunning`. Then call `record_dunning_event` with `first_notice` or `overdue_notice`. Reminders do not capture money or post journals.

## Project a payment intent

```bash
python3 -c "from metering_billing import PaymentIntentService"
```

After a `collection_case` exists, call `PaymentIntentService.project_payment_intent` with the tenant and `collection_case_id`, or `POST /v1/payment-intents`. The intent amount equals the exact case outstanding. An identical replay returns the same `payment_intent_id`. Status stays `projected`. The intent does not capture, settle, store a card PAN, or post a journal.

## Present a payment intent

```bash
python3 -c "from metering_billing import PaymentIntentPresentmentService"
# GET /v1/payment-intents/{payment_intent_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/payment-intents?tenant_reference=urn:cwl:tenant_001
```

After a `payment_intent` exists, `GET /v1/payment-intents/{payment_intent_id}` returns the tenant-scoped statement. Create a projected payment intent, then record the receipt.

## Record a payment receipt

```bash
python3 -c "from metering_billing import PaymentSettlementService"
```

After a projected `payment_intent` exists, call `PaymentSettlementService.record_payment_receipt` with the tenant, `payment_intent_id`, and exact `received_amount`, or `POST /v1/payment-receipts`. The receipt status is `applied`. The linked collection-case outstanding is reduced by the same amount; remaining zero marks the case `settled`. An identical replay returns the same `payment_receipt_id`. Accept also proposes the existing cash journal. Call `cancel_payment_intent` to flip a projected intent to `cancelled` without writing a receipt. The receipt does not capture via a provider or post a journal. `POST /v1/cash-journal-proposals` remains a manual replay.

## Present a payment receipt

```bash
python3 -c "from metering_billing import PaymentReceiptPresentmentService"
# GET /v1/payment-receipts/{payment_receipt_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/payment-receipts?tenant_reference=urn:cwl:tenant_001
```

After a `payment_receipt` exists, `GET /v1/payment-receipts/{payment_receipt_id}` returns the tenant-scoped statement. Record the receipt; the cash journal is already validated for AIS to pull.

```http
# POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash
# GET /v1/unapplied-cash/{unapplied_cash_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/unapplied-cash?tenant_reference=urn:cwl:tenant_001
```

After a stored `payment_receipt` exists, `POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash` parks leftover remittance without rewriting #12 overpay rejection. Replay of the same tenant and receipt returns the same `unapplied_cash_id`. `GET /v1/unapplied-cash/{unapplied_cash_id}` returns the tenant-scoped statement. Do not auto-apply leftover to another case. The path does not invent a journal, webhook, write-off, settlement, credit note, or AIS call.

```http
# POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications
# GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/unapplied-cash-applications?tenant_reference=urn:cwl:tenant_001
```

After leftover is parked, `POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications` applies the full parked amount onto another open case. Replay of the same tenant and leftover returns the same `unapplied_cash_application_id`. First successful apply enqueues one existing `unapplied_cash.applied` webhook outbox event. Remaining zero does not settle; #46 remains the explicit settle-when-zero command. `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` returns the tenant-scoped statement. After apply exists, `POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals` composes one validated unapplied-cash/AR journal. The path does not invent a write-off, credit note, AIS call, settlement command, or second webhook system.
After leftover is parked and unused, `POST /v1/unapplied-cash/{unapplied_cash_id}/refunds` records a commercial refund of the full parked amount. Replay of the same tenant and leftover returns the same `unapplied_cash_refund_id`. First successful refund enqueues one existing `refund.recorded` webhook outbox event. The parked leftover row stays `parked`. `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` returns the tenant-scoped statement. After the refund exists, `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals` composes one validated unapplied-cash/cash journal for AIS to pull. The path does not invent a write-off, settlement, credit note, PSP capture, AIS call, statutory numbering, or second webhook system.

## Present a credit adjustment

```bash
python3 -c "from metering_billing import CreditAdjustmentPresentmentService"
# GET /v1/credit-adjustments/{credit_adjustment_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/credit-adjustments?tenant_reference=urn:cwl:tenant_001
```

After a `credit_adjustment` exists, `GET /v1/credit-adjustments/{credit_adjustment_id}` returns the tenant-scoped statement. Record the credit; AIS pulls the validated journal.

## Accept writes over HTTP

```bash
python3 -c "from metering_billing.http_app import create_http_app"
python3 -m metering_billing.http_app
```

`create_http_app(ledger=...)` is a thin stdlib WSGI adapter over the services above. Standalone serving binds `0.0.0.0:$PORT` (default 8000). Every write requires `tenant_reference`. Money stays exact-decimal strings. HTTP 200 means `accepted` or `duplicate_replay` on writes, or a successful read. HTTP 422 means `rejected` or an unreadable request. HTTP 404 is an unknown route or an unknown/cross-tenant proposal, credit, observation, rate-card version, usage event, rating run, invoice draft, collection case, payment intent, payment receipt, or API credential. The adapter does not post journals or call a named payment provider.

The bound ledger uses PostgreSQL whenever `METERING_BILLING_POSTGRES_DSN` is
set to a non-empty value and the backend selector is unset or `postgres`. Set
the selector explicitly to `postgres` for clarity, or set it to `memory` only
when the deterministic reference adapter is intentional. PostgreSQL selection
fails closed when the DSN is missing or empty:

```bash
export METERING_BILLING_LEDGER_BACKEND=postgres
export METERING_BILLING_POSTGRES_DSN="postgresql:///metering_billing?host=/tmp&port=5433"
python3 -c "from metering_billing.http_app import create_default_ledger, create_http_app; app = create_http_app(create_default_ledger())"
```

In a deployed process, a non-empty `METERING_BILLING_POSTGRES_DSN` value alone
is enough to select PostgreSQL; `METERING_BILLING_LEDGER_BACKEND=memory` is
the explicit test/reference override. With the backend unset and the DSN absent
or empty, the memory adapter is selected. An unsupported backend value fails
closed at startup rather than falling back to memory.

Unauthenticated `GET /healthz` stays a static liveness reply, and `GET /readyz` reports the serving backend: healthy processes answer `200 {"status": "ready", "backend": "memory" | "postgres"}` while a failing PostgreSQL probe answers `503 {"status": "not_ready", "backend": ..., "reason": "migration_history_unavailable"}` with stable reason codes only (see ADR 0123).

Until a tenant has an active API credential, the existing tenant pin is enough (bootstrap window). AIS can keep pulling with `X-CWL-Tenant-Reference` until a key is issued for that tenant. After a key exists, send it on every `/v1` call.

## Issue a tenant API credential

```bash
python3 -c "from metering_billing import TenantApiCredentialService"
# POST /v1/tenant-api-credentials
# GET /v1/tenant-api-credentials/{tenant_api_credential_id}
# GET /v1/tenant-api-credentials
# POST /v1/tenant-api-credentials/{id}/revoke
```

Call `TenantApiCredentialService.issue_credential` with a tenant and an optional two-or-more-word `snake_case` `credential_label`. The response includes the secret once. The ledger stores only a keyed HMAC. A second issue always mints a new secret. After one or more active keys exist, every `/v1` write and GET except credential issue requires `Authorization: Bearer <secret>` or `X-CWL-Api-Key: <secret>` whose tenant equals `X-CWL-Tenant-Reference` / `tenant_reference`. `GET /v1/tenant-api-credentials/{tenant_api_credential_id}` and `GET /v1/tenant-api-credentials` present stored metadata as `{tenant_api_credentials, next_cursor}` and never the secret or hash. `GET /healthz` stays unauthenticated. Issue a key, then send it on every `/v1` call; revoke when leaked.

## Register a webhook callback

```bash
python3 -c "from metering_billing import WebhookSubscriptionService, WebhookDeliveryService"
# POST /v1/webhook-subscriptions
# GET /v1/webhook-subscriptions
# GET /v1/webhook-subscriptions/{webhook_subscription_id}
# POST /v1/webhook-subscriptions/{id}/revoke
# POST /v1/webhook-deliveries
# GET /v1/webhook-outbox-events/{outbox_event_id}
# GET /v1/webhook-outbox-events
# GET /v1/webhook-deliveries/{delivery_attempt_id}
# GET /v1/webhook-deliveries
```

Register an https callback, then run deliveries; AIS may keep polling. `WebhookSubscriptionService.register_subscription` accepts a tenant, https `callback_url`, and a closed event-type set (`journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, `invoice.voided`, `credit_note.issued`, `credit_note.voided`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`, `dispute.held`, `dispute.released`, `spend_budget.published`, `spend_budget.over`, `spend_budget.approaching`). http is allowed only for localhost tests. The secret is returned once and is process-local; PostgreSQL stores only the prefix and keyed hash. Replay of the same tenant, URL, event set, and contract version returns the same `webhook_subscription_id`. `WebhookSubscriptionPresentmentService` projects stored metadata. `GET /v1/webhook-subscriptions/{webhook_subscription_id}` and `GET /v1/webhook-subscriptions` present `{webhook_subscriptions, next_cursor}` and never return the secret, hash, prefix, or signed body. Accepted commercial facts append `webhook_outbox_event` rows. `WebhookOutboxEventPresentmentService` projects stored commercial outbox metadata. `GET /v1/webhook-outbox-events/{outbox_event_id}` and `GET /v1/webhook-outbox-events` present `{webhook_outbox_events, next_cursor}` and never return `payload_json`, the webhook secret, hash, prefix, or signature. `POST /v1/webhook-deliveries` POSTs the envelope and signs the raw body with `X-CWL-Webhook-Signature: sha256=<hex>`; PostgreSQL persists each attempt and the delivered transition. `GET /v1/webhook-deliveries/{delivery_attempt_id}` and `GET /v1/webhook-deliveries` present stored `webhook_delivery_attempt` rows as `{webhook_deliveries, next_cursor}` and never resend or return the secret, hash, or signed body. This path does not flip `proposal_status` or call AIS posting-receipt.

## Present an invoice draft

```bash
python3 -c "from metering_billing import InvoicePresentmentService"
# GET /v1/invoice-drafts/{invoice_draft_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/invoice-drafts?tenant_reference=urn:cwl:tenant_001
```

After an `invoice_draft` exists, `GET /v1/invoice-drafts/{invoice_draft_id}` returns the tenant-scoped statement. Open the draft statement, then collect or credit.

## Issue a commercial invoice

```bash
python3 -c "from metering_billing import IssuedInvoiceService, IssuedInvoicePresentmentService"
# POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices
# GET /v1/issued-invoices/{issued_invoice_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-invoices?tenant_reference=urn:cwl:tenant_001
```

After an `invoice_draft` exists, `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` writes one immutable commercial snapshot. `GET /v1/issued-invoices/{issued_invoice_id}` returns the tenant-scoped statement, including optional stored `tax_assessment_id` when the draft assessment still matches the frozen totals. Issue invoice, then collect or credit.

## Void an unused issued invoice

```bash
python3 -c "from metering_billing import IssuedInvoiceVoidService, IssuedInvoiceVoidPresentmentService"
# POST /v1/issued-invoices/{issued_invoice_id}/voids
# GET /v1/issued-invoice-voids/{issued_invoice_void_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-invoice-voids?tenant_reference=urn:cwl:tenant_001
```

After an unused `issued_invoice` exists, `POST /v1/issued-invoices/{issued_invoice_id}/voids` records one commercial void. Replay of the same tenant and issued invoice returns the stored `issued_invoice_void_id`. An unused open or dunning collection case closes as `voided`. First successful void enqueues one existing `invoice.voided` webhook outbox event. `GET /v1/issued-invoice-voids/{issued_invoice_void_id}` returns the tenant-scoped statement. This path does not invent a journal, second webhook system, AIS call, or statutory identifier.

## Issue a commercial credit note

```bash
python3 -c "from metering_billing import IssuedCreditNoteService, IssuedCreditNotePresentmentService"
# POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes
# GET /v1/issued-credit-notes/{issued_credit_note_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-credit-notes?tenant_reference=urn:cwl:tenant_001
```

After a `credit_adjustment` exists, `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` writes one immutable commercial snapshot. `GET /v1/issued-credit-notes/{issued_credit_note_id}` returns the tenant-scoped statement, including optional stored `tax_assessment_id` when the draft assessment still reproduces the frozen credit split. Issue the credit note; the validated journal remains available for AIS.

## Void an unused issued credit note

```bash
python3 -c "from metering_billing import IssuedCreditNoteVoidService, IssuedCreditNoteVoidPresentmentService"
# POST /v1/issued-credit-notes/{issued_credit_note_id}/voids
# POST /v1/issued-credit-note-voids/{issued_credit_note_void_id}/journal-proposals
# GET /v1/issued-credit-note-voids/{issued_credit_note_void_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/issued-credit-note-voids?tenant_reference=urn:cwl:tenant_001
```

After an unused `issued_credit_note` exists, `POST /v1/issued-credit-notes/{issued_credit_note_id}/voids` records one commercial void. Replay of the same tenant and issued credit note returns the stored `issued_credit_note_void_id`. Collection remaining is unchanged because the note was never applied. After a void exists, apply fail-closes as `issued_credit_note_voided`. First successful void enqueues one existing `credit_note.voided` webhook outbox event. `GET /v1/issued-credit-note-voids/{issued_credit_note_void_id}` returns the tenant-scoped statement. The void write does not compose a journal. After a void exists, `POST /v1/issued-credit-note-voids/{issued_credit_note_void_id}/journal-proposals` composes one validated AR/revenue reverse of the original credit journal for AIS to pull. This path does not invent a second webhook system, AIS call, VAT register, or statutory identifier.

## Apply an issued credit note to a collection case

```bash
python3 -c "from metering_billing import CreditNoteApplicationService, CreditNoteApplicationPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/credit-note-applications
# GET /v1/credit-note-applications/{credit_note_application_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/credit-note-applications?tenant_reference=urn:cwl:tenant_001
```

After an `issued_credit_note` and an open `collection_case` exist for the same invoice, `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` reduces outstanding by the exact issued inclusive amount. First successful apply enqueues one `credit_note.applied` outbox event. `GET /v1/credit-note-applications/{credit_note_application_id}` returns the tenant-scoped statement. Apply the issued credit note, then collect the residual.

## Settle a zero-outstanding collection case

```bash
python3 -c "from metering_billing import CollectionCaseSettlementService, CollectionCaseSettlementPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/settlements
# GET /v1/collection-case-settlements/{collection_case_settlement_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-case-settlements?tenant_reference=urn:cwl:tenant_001
```

After an open `collection_case` already shows exact-zero outstanding, `POST /v1/collection-cases/{collection_case_id}/settlements` flips status to `settled` without inventing a receipt or write-off. First successful settle enqueues one `collection.settled` outbox event. `GET /v1/collection-case-settlements/{collection_case_settlement_id}` returns the tenant-scoped statement. Settle the zero-outstanding case, then wait.

## Hold a disputed collection case

```bash
python3 -c "from metering_billing import CollectionDisputeService, CollectionDisputePresentmentService"
# POST /v1/collection-cases/{collection_case_id}/disputes
# GET /v1/collection-disputes/{collection_dispute_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-disputes?tenant_reference=urn:cwl:tenant_001
```

After an open or dunning `collection_case` is disputed, `POST /v1/collection-cases/{collection_case_id}/disputes` flips status to `disputed` without changing remaining outstanding. Replay returns the stored `collection_dispute_id`. New dunning and money-close commands fail closed while held. `GET /v1/collection-disputes/{collection_dispute_id}` returns the tenant-scoped statement. Hold the disputed case, then wait.

## Release a held collection dispute

```bash
python3 -c "from metering_billing import CollectionDisputeReleaseService, CollectionDisputeReleasePresentmentService"
# POST /v1/collection-disputes/{collection_dispute_id}/releases
# GET /v1/collection-dispute-releases/{collection_dispute_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-dispute-releases?tenant_reference=urn:cwl:tenant_001
```

After a held `collection_dispute` exists, `POST /v1/collection-disputes/{collection_dispute_id}/releases` flips the hold to `released` and restores the case to `open` or `dunning` without changing remaining outstanding. Replay returns the stored `collection_dispute_id`. After release, dunning and money-close commands follow the existing open-case rules. `GET /v1/collection-dispute-releases/{collection_dispute_id}` returns the tenant-scoped statement. Release the hold, then collect or dunn.

## Write off leftover collection remaining

```bash
python3 -c "from metering_billing import CollectionWriteOffService, CollectionWriteOffPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/write-offs
# GET /v1/collection-write-offs/{collection_write_off_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-write-offs?tenant_reference=urn:cwl:tenant_001
```

After an open `collection_case` still shows leftover remaining, `POST /v1/collection-cases/{collection_case_id}/write-offs` zeros remaining without inventing a receipt, credit, or settlement. First successful write-off enqueues one `write_off.recorded` outbox event. `GET /v1/collection-write-offs/{collection_write_off_id}` returns the tenant-scoped statement. `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals` composes one validated write-off/AR journal for AIS to pull. Write off leftover remaining, compose the journal, then settle.

## Present a collection case

```bash
python3 -c "from metering_billing import CollectionCasePresentmentService"
# GET /v1/collection-cases/{collection_case_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/collection-cases?tenant_reference=urn:cwl:tenant_001
```

After a `collection_case` exists, `GET /v1/collection-cases/{collection_case_id}` returns the tenant-scoped statement. Open the collection case, then collect or credit.

## Present collection aging

```bash
python3 -c "from metering_billing import CollectionAgingPresentmentService"
# GET /v1/collection-aging?tenant_reference=urn:cwl:tenant_001
```

After open or dunning `collection_case` rows exist, `GET /v1/collection-aging` returns current / 1-30 / 31-60 / 61-90 / 90+ outstanding grouped by `currency_code`. Settled cases and exact-zero remaining are omitted. Open the aging statement, then collect or credit. This path does not invent a journal, write-off, settlement, payment, or AIS call.

## Present a billing-account statement

```bash
python3 -c "from metering_billing import AccountStatementPresentmentService"
# GET /v1/billing-accounts/{billing_account_id}/statement
# Header: X-CWL-Tenant-Reference: urn:cwl:tenant_001
```

After issued invoices, unused issued-invoice voids, collection cases, credit-note applications, unused issued-credit-note voids, write-offs, parked leftover, or leftover refunds exist, `GET /v1/billing-accounts/{billing_account_id}/statement` returns those stored totals grouped by `currency_code`. `issued_invoice_total` stays the issued snapshot. `applied_credit_total` stays applied credits only. Unused voids are `voided_invoice_total` and `voided_credit_total`. Missing account is HTTP 404. Cross-tenant account is HTTP 403. Open the account statement, then collect, credit, park, apply, or refund. This path does not invent a journal, webhook, AIS call, or statutory identifier.

## Present rated spend by product

```bash
python3 -c "from metering_billing import RatedSpendPresentmentService"
# GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=2026-08-16T10:00:00Z&window_ended_at=2026-08-16T11:00:00Z
# GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=2026-08-16T10:00:00Z&window_ended_at=2026-08-16T11:00:00Z&group_by=project
# GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=2026-08-16T10:00:00Z&window_ended_at=2026-08-16T11:00:00Z&group_by=credential
# GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=2026-08-16T10:00:00Z&window_ended_at=2026-08-16T11:00:00Z&group_by=principal
# GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=2026-08-16T10:00:00Z&window_ended_at=2026-08-16T11:00:00Z&group_by=cost_center
# Header: X-CWL-Tenant-Reference: urn:cwl:tenant_001
```

After a `rating_run` exists for that half-open window, `GET /v1/billing-accounts/{billing_account_id}/rated-spend` returns stored rated or exclusive draft-line amounts grouped by `product_code`. Optional `group_by=project` adds stored exclusive-account `project_reference` and omits usage without that URN. Optional `group_by=credential` adds stored exclusive-account `credential_reference` and omits usage without that URN. Optional `group_by=principal` adds stored exclusive-account `billing_principal_reference`. Mixed or unresolved principals omit the run. Optional `group_by=cost_center` adds stored exclusive-account `cost_center_reference` and omits usage without that URN. Mixed cost centers omit the run. `rated_amount` is the stored line amount as an exact Decimal string. Unrated usage is omitted. Mixed-account and lineless drafts are omitted. The read does not re-rate or write money. Missing account is HTTP 404. Cross-tenant account is HTTP 403. An illegal window or unknown `group_by` is HTTP 422. Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice.

## Publish a spend budget

```bash
python3 -c "from metering_billing import SpendBudgetService, SpendBudgetPresentmentService"
# POST /v1/billing-accounts/{billing_account_id}/spend-budgets
# GET /v1/spend-budgets/{spend_budget_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/spend-budgets?tenant_reference=urn:cwl:tenant_001
# GET /v1/spend-budgets/{spend_budget_id}/evaluation?tenant_reference=urn:cwl:tenant_001
# POST /v1/spend-budgets/{spend_budget_id}/over-signal
# Header: X-CWL-Tenant-Reference: urn:cwl:tenant_001
```

Call `SpendBudgetService.publish_spend_budget` with a tenant, `billing_account_id`, ISO 4217 currency, an exact `budget_amount` greater than zero, and the same half-open ISO 8601 window as rated spend. `POST /v1/billing-accounts/{billing_account_id}/spend-budgets` stays that command and refuses PAN and provider secrets. Replay of the same identity returns the stored `spend_budget_id`. A later distinct amount appends a new row. First successful publish enqueues one existing `spend_budget.published` webhook outbox event. After a `spend_budget` exists, `GET /v1/spend-budgets/{spend_budget_id}` returns the tenant-scoped statement. `GET /v1/spend-budgets` lists `{spend_budgets, next_cursor}`. `GET /v1/spend-budgets/{spend_budget_id}/evaluation` compares the published budget to already-rated spend. `POST /v1/spend-budgets/{spend_budget_id}/over-signal` observes that same math and enqueues one existing `spend_budget.over` outbox event when utilization is first over. under and at write zero over-signal rows. `POST /v1/spend-budgets/{spend_budget_id}/approaching-signal` observes that same math and enqueues one existing `spend_budget.approaching` outbox event when utilization is first `at` the documented `budget_amount`. under and over write zero approaching-signal rows. Missing account is HTTP 404. Cross-tenant account is HTTP 403. Publish the commercial budget, then wait. Observe first-over or first-at utilization, then run deliveries. The evaluation GET does not persist or enqueue. The over-signal and approaching-signal writes do not persist an evaluation snapshot, stop rating, or compose a journal.

## Inspect billing-account budget status

```bash
python3 -c "from metering_billing import SpendBudgetEvaluationPresentmentService"
# GET /v1/billing-accounts/{billing_account_id}/budget-status?tenant_reference=urn:cwl:tenant_001
# GET /v1/spend-budgets/{spend_budget_id}/evaluation?tenant_reference=urn:cwl:tenant_001
# Header: X-CWL-Tenant-Reference: urn:cwl:tenant_001
```

After one or more `spend_budget` rows exist for that account, `GET /v1/billing-accounts/{billing_account_id}/budget-status` lists `{budget_statuses, next_cursor}` using the same rated-spend plus exact remaining/over math as `GET /v1/spend-budgets/{spend_budget_id}/evaluation`. Order is `published_at` then `spend_budget_id`. `page_limit` defaults to 50 and maxes at 100. Each row keeps its own currency. Missing account is HTTP 404. Cross-tenant account is HTTP 403. Unknown or cross-tenant budgets are omitted. Inspect remaining, then wait. This path does not persist, mutate the budget, stop rating, or compose a journal.

## Present a dunning notice

```bash
python3 -c "from metering_billing import DunningEventPresentmentService"
# POST /v1/collection-cases/{collection_case_id}/dunning-events
# GET /v1/dunning-events/{dunning_event_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/dunning-events?tenant_reference=urn:cwl:tenant_001
```

After a `collection_dunning_event` exists, `GET /v1/dunning-events/{dunning_event_id}` returns the tenant-scoped reminder. `GET /v1/dunning-events` lists `{dunning_events, next_cursor}`. Record the commercial reminder, then collect or credit. This path does not send mail or invent recipient PII.

## Present a statement in Storybook

```bash
cd operator_console
npm install
npm run storybook
```

`operator_console` renders the #21 invoice statement, the issued-invoice statement, the issued-credit-note statement, the credit-note-application statement, the collection-case-settlement statement, the collection-case statement, the collection-aging statement, the account statement, the rated-spend statement, the spend-budget statement, the account budget-status statement, the payment-intent statement, the payment-receipt statement, the credit-adjustment statement, the rate-card statement, the usage-event statement, the rating-run statement, the tax-assessment statement, and the posting-receipt observation statement with tokenized status chip and tenant pin. Amounts stay exact-decimal strings. Customer copy on an account statement is: open the account statement, then collect, credit, park, apply, or refund. Customer copy on rated spend is: inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice. Customer copy on account budget status is: open the account budget status, then wait. Storybook is the UI surface for this slice. The package is importable and does not replace `metering_billing`.

## Pull journal proposals

```bash
# GET /v1/journal-proposals?tenant_reference=urn:cwl:tenant_001
# GET /v1/journal-proposals/{proposal_id}?tenant_reference=urn:cwl:tenant_001
```

AIS pulls validated proposals from the same stdlib app. Pin the tenant with optional `X-CWL-Tenant-Reference` or with `tenant_reference` in the query or JSON body. If both are present they must match. That pull keeps working until a key is issued for the tenant; after issue, send the key on every `/v1` call. Cash, AR, and credit proposals share `journal_proposal` and appear in the same list. Query never marks a proposal exported, posted, or consumed. Billing emits semantic account roles only; AIS maps `cash_receipt` to its own chart.

## Pull a posting receipt

```bash
python3 -c "from metering_billing import PostingReceiptPullService"
# POST /v1/posting-receipt-observations
# GET /v1/posting-receipt-observations/{idempotency_key}?tenant_reference=urn:cwl:tenant_001
# GET /v1/posting-receipt-observations?tenant_reference=urn:cwl:tenant_001
```

After AIS accepts a validated proposal, an operator pulls the AIS `posting_receipt` and Billing stores it as a commercial observation. If AIS returns 404, accept the proposal on AIS and retry. `posting_status_code` stays an AIS fact. Billing `proposal_status` stays `validated`. `GET /v1/posting-receipt-observations/{idempotency_key}` stays the existing #16 item read and does not call AIS. `GET /v1/posting-receipt-observations` lists `{posting_receipt_observations, next_cursor}`. Drain AIS outbox, then store the receipt observation.

## Drain the AIS outbox

```bash
python3 -c "from metering_billing import AisOutboxDrainService"
# POST /v1/ais-outbox-drains
```

Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty. `AisOutboxDrainService.drain_ais_outbox` GETs `GET /outbox-events?event_type_code=posting_receipt`, matches constructed `urn:cwl:accounting:posting_receipt:{proposal_id}` and `urn:cwl:accounting:general_journal:{proposal_id}` by equality, then GETs `/posting-receipts?idempotency_key=` with the stored Billing key. Empty unpublished pages skip receipt GETs. After a stored observation, POST `/outbox-events/{outbox_event_id}/publish`. This path does not parse the payload URN, drain `journal_reversal` or `period_close`, or flip `proposal_status`.

## Record a credit adjustment

```bash
python3 -c "from metering_billing import CreditAdjustmentService"
# POST /v1/credit-adjustments
# GET /v1/credit-adjustments/{credit_adjustment_id}
```

After an `invoice_draft` exists, call `CreditAdjustmentService.record_credit_adjustment` with the tenant, `invoice_draft_id`, exact `credit_amount`, and a closed `credit_reason_code` (`rating_correction`, `goodwill`, or `billing_error`), or `POST /v1/credit-adjustments`. The credit cannot exceed remaining adjustable consideration. If a collection case exists, outstanding is reduced by the same inclusive amount; remaining zero marks the case `settled`. When a tax assessment exists, the credit is split proportionally and the journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`. Untaxed credits stay two-line. An identical replay returns the same `credit_adjustment_id` and `proposal_id`. Record the credit; AIS pulls the validated journal. This path does not post, call AIS, refund-to-card, or chargeback.

## Publish a tax rate and assess a draft

```bash
python3 -c "from metering_billing import TaxRateService, TaxAssessmentService"
# POST /v1/tax-assessments
# GET /v1/tax-assessments/{tax_assessment_id}?tenant_reference=urn:cwl:tenant_001
# GET /v1/tax-assessments?tenant_reference=urn:cwl:tenant_001
```

Call `TaxRateService.publish_tax_rate` with a tenant, a closed `tax_code` (`vat`, `gst`, or `sales_tax`), and an exact `tax_rate` in `[0, 1]`. Then call `TaxAssessmentService.assess_tax` with the tenant, `invoice_draft_id`, and that persisted version, or `POST /v1/tax-assessments`. Tax is half-even rounded to the documented ISO 4217 minor units. Assess before opening a collection case. A taxed `propose_journal` credits semantic `tax_payable`; AIS must map that role. After a `tax_assessment` exists, `GET /v1/tax-assessments/{tax_assessment_id}` stays the existing #19 item read. `GET /v1/tax-assessments` lists `{tax_assessments, next_cursor}`. Publish a tax rate, assess the draft, then propose the journal and let AIS pull.

## Next action

Open the draft statement in Storybook, then collect or credit. Amounts stay exact-decimal strings. Do not add a production SPA, PDF, or email in this slice.
