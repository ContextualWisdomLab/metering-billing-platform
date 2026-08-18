# Storybook inventory

Operator presentment stories live in `operator_console/stories/`.  Run them with `cd operator_console && npm install && npm run storybook`.  Storybook is the UI surface for this slice.

Customer copy on every invoice statement: amount due and the next operator action, **Collect or credit**.

Customer copy on every collection case: outstanding and the next operator action. Open the collection case, then collect or credit.

Customer copy on every payment intent: projected amount and the next operator action. Create a projected payment intent, then record the receipt.

Customer copy on every payment receipt: received amount and the next operator action. Record the receipt; the cash journal is already validated for AIS to pull.

Customer copy on every credit adjustment: credited amount and the next operator action. Record the credit; AIS pulls the validated journal.

Customer copy on every rate card: unit price and the next operator action. Publish a rate card, then rate a window against that version.

Customer copy on every usage event: quantity and the next operator action. Ingest usage, then rate a window against a published card.

Customer copy on every rating run: rated total and the next operator action. Rate a window, then draft an invoice.

Customer copy on every tax assessment: tax-inclusive amount and the next operator action. Publish a tax rate, assess the draft, then propose the journal and let AIS pull.

Customer copy on every posting-receipt observation: AIS posting status and the next operator action. Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.

Customer copy on every webhook delivery: stored attempt outcome and the next operator action. Register an https callback, then run deliveries; AIS may keep polling.

Customer copy on every tenant API credential: prefix, status, and the next operator action. Issue a key, then send it on every /v1 call; revoke when leaked.

Customer copy on every webhook subscription: callback URL, status, and the next operator action. Register an https callback, then run deliveries; AIS may keep polling.

Customer copy on every dunning notice: notice code, sequence, and the next operator action. Record the commercial reminder, then collect or credit.

Customer copy on every webhook outbox event: event type, delivery status, and the next operator action. Register an https callback, then run deliveries; AIS may keep polling.

Customer copy on every issued invoice: frozen inclusive total and the next operator action. Issue invoice, then collect or credit.

Customer copy on every issued credit note: frozen inclusive credit and the next operator action. Issue the credit note; the validated journal remains available for AIS.

Customer copy on every credit-note application: applied amount, remaining outstanding, and the next operator action. Apply the issued credit note, then collect the residual.

Customer copy on every collection-case settlement: exact-zero remaining and the next operator action. Settle the zero-outstanding case, then wait.

Customer copy on every account statement: remaining and the next operator action. Open the account statement, then collect, credit, park, apply, or refund.

## Stories

| Story | Module | Fixtures |
| --- | --- | --- |
| InvoiceStatement | `src/invoice_statement.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| AmountDue | `src/amount_due.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| LineTable | `src/line_table.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| StatusChip | `src/status_chip.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| CollectionCase | `src/collection_case.js` | `open_collection_case.json`, `dunning_collection_case.json`, `settled_collection_case.json` |
| CollectionAging | `src/collection_aging.js` | `morning_collection_aging.json` |
| PaymentIntent | `src/payment_intent.js` | `projected_payment_intent.json`, `cancelled_payment_intent.json` |
| PaymentReceipt | `src/payment_receipt.js` | `applied_full_payment_receipt.json`, `applied_partial_payment_receipt.json` |
| CreditAdjustment | `src/credit_adjustment.js` | `recorded_morning_credit.json`, `recorded_taxed_credit.json` |
| RateCard | `src/rate_card.js` | `published_standard_rate.json`, `published_premium_rate.json` |
| UsageEvent | `src/usage_event.js` | `stored_morning_usage.json`, `stored_partial_token_usage.json` |
| RatingRun | `src/rating_run.js` | `rated_morning_window.json`, `rated_partial_window.json` |
| TaxAssessment | `src/tax_assessment.js` | `assessed_morning_vat.json`, `assessed_partial_vat.json` |
| PostingReceiptObservation | `src/posting_receipt_observation.js` | `observed_posted_morning.json`, `observed_held_receipt.json` |
| WebhookDelivery | `src/webhook_delivery.js` | `delivered_morning.json`, `failed_callback.json` |
| TenantApiCredential | `src/tenant_api_credential.js` | `active_operator_key.json`, `revoked_leaked_key.json` |
| WebhookSubscription | `src/webhook_subscription.js` | `active_https_callback.json`, `revoked_https_callback.json` |
| DunningNotice | `src/dunning_notice.js` | `first_notice_morning.json`, `overdue_notice_evening.json` |
| WebhookOutboxEvent | `src/webhook_outbox_event.js` | `pending_journal_validated.json`, `delivered_receipt_applied.json` |
| IssuedInvoice | `src/issued_invoice.js` | `issued_untaxed_morning.json`, `issued_taxed_hundred.json` |
| IssuedCreditNote | `src/issued_credit_note.js` | `issued_morning_credit_note.json`, `issued_taxed_credit_note.json` |
| CreditNoteApplication | `src/credit_note_application.js` | `applied_morning_credit_note.json` |
| CollectionCaseSettlement | `src/collection_case_settlement.js` | `settled_morning_zero.json` |
| AccountStatement | `src/account_statement.js` | `settled_account_statement.json`, `voided_account_statement.json` |

The tenant pin is a tokenized module composed into `InvoiceStatement`.  It is not a one-off style.

## Fixtures

| File | Meaning | Exact strings |
| --- | --- | --- |
| `taxed_partial_credit.json` | VAT assessed, goodwill credit 11.00 | `amount_due` `99.00`, inclusive `110.00` |
| `untaxed_morning.json` | Known morning tokens, no tax | `amount_due` `0.003705`, tax `0` |
| `settled_statement.json` | Full inclusive credit | `amount_due` `0.00` |
| `open_collection_case.json` | Open morning case | `collection_outstanding` `0.003705`, action `collect` |
| `dunning_collection_case.json` | First notice sent | `collection_outstanding` `100.00`, last `first_notice` |
| `settled_collection_case.json` | Settled case | `collection_outstanding` `0.00`, action `wait` |
| `morning_collection_aging.json` | Morning USD aging | current `0.003705`, 1-30 `1.25`, 90+ `8.00` |
| `projected_payment_intent.json` | Projected morning intent | `payment_amount` `0.003705`, action `record_receipt` |
| `cancelled_payment_intent.json` | Cancelled intent | `payment_amount` `0.003705`, action `wait` |
| `applied_full_payment_receipt.json` | Full morning receipt | `received_amount` `0.003705`, remaining `0.00`, action `drain_or_wait` |
| `applied_partial_payment_receipt.json` | Partial receipt | `received_amount` `0.001`, remaining `0.002705`, action `record_receipt` |
| `recorded_morning_credit.json` | Full morning credit | `credit_amount` `0.003705`, tax `0`, action `wait` |
| `recorded_taxed_credit.json` | Taxed goodwill credit | `credit_amount` `11.00`, exclusive `10.00`, tax `1.00`, action `wait` |
| `published_standard_rate.json` | Standard token card | `unit_amount` `0.000002`, action `rate_window` |
| `published_premium_rate.json` | Premium token card | `unit_amount` `0.000005`, action `rate_window` |
| `stored_morning_usage.json` | Known morning tokens | `quantity` `1810`, action `rate_window` |
| `stored_partial_token_usage.json` | Partial token event | `quantity` `42.5`, action `rate_window` |
| `rated_morning_window.json` | Known morning window | `rated_total_amount` `0.003705`, action `draft_invoice` |
| `rated_partial_window.json` | Partial token window | `rated_total_amount` `0.000085`, action `draft_invoice` |
| `assessed_morning_vat.json` | Known 10 percent VAT | `tax_inclusive_amount` `110.00`, action `propose_journal` |
| `assessed_partial_vat.json` | Partial 10 percent VAT | `tax_inclusive_amount` `22.00`, action `propose_journal` |
| `observed_posted_morning.json` | AIS posted observation | `posting_status_code` `posted`, action `wait` |
| `observed_held_receipt.json` | AIS held observation | `posting_status_code` `held`, action `wait` |
| `delivered_morning.json` | Stored success attempt | `http_status` `200`, action `wait` |
| `failed_callback.json` | Stored failure attempt | `failure_reason_code` `webhook_http_error`, action `run_deliveries` |
| `active_operator_key.json` | Active inventory key | prefix `cwlak_fake001`, action `wait` |
| `revoked_leaked_key.json` | Revoked inventory key | prefix `cwlak_fake002`, action `issue` |
| `active_https_callback.json` | Active https callback | URL `https://hooks.example.test/cwl`, action `run_deliveries` |
| `revoked_https_callback.json` | Revoked https callback | URL `https://hooks.example.test/cwl-revoked`, action `register` |
| `first_notice_morning.json` | First commercial reminder | notice `first_notice`, action `collect` |
| `overdue_notice_evening.json` | Overdue commercial reminder | notice `overdue_notice`, action `collect` |
| `pending_journal_validated.json` | Pending commercial outbox event | type `journal_proposal.validated`, action `run_deliveries` |
| `delivered_receipt_applied.json` | Delivered commercial outbox event | type `payment_receipt.applied`, action `wait` |
| `issued_untaxed_morning.json` | Issued morning snapshot | `tax_inclusive_amount` `0.003705`, action `collect` |
| `issued_taxed_hundred.json` | Issued taxed snapshot | `tax_inclusive_amount` `110.00`, action `collect` |
| `issued_morning_credit_note.json` | Issued morning credit note | `tax_inclusive_amount` `0.003705`, action `wait` |
| `issued_taxed_credit_note.json` | Issued taxed credit note | `tax_inclusive_amount` `11.00`, exclusive `10.00`, tax `1.00`, action `wait` |
| `applied_morning_credit_note.json` | Applied morning credit note | `applied_amount` `0.003705`, remaining `0`, action `wait` |
| `settled_morning_zero.json` | Settled morning zero-outstanding case | remaining `0`, action `wait` |
| `settled_account_statement.json` | Settled billing-account statement | remaining `0.00`, applied `110.00`, voids `0` |
| `voided_account_statement.json` | Unused invoice and credit voids | issued `110.00`, voided invoice `110.00`, voided credit `11.00` |

Amounts are canonical decimal strings from `schemas/invoice-draft-presentment.schema.json`, `schemas/collection-case-presentment.schema.json`, `schemas/payment-intent-presentment.schema.json`, `schemas/payment-receipt-presentment.schema.json`, `schemas/credit-adjustment-presentment.schema.json`, `schemas/rate-card-presentment.schema.json`, `schemas/usage-event-presentment.schema.json`, `schemas/rating-run-presentment.schema.json`, `schemas/tax-assessment-presentment.schema.json`, `schemas/posting-receipt-observation-presentment.schema.json`, `schemas/webhook-delivery-presentment.schema.json`, `schemas/tenant-api-credential-presentment.schema.json`, `schemas/webhook-subscription-presentment.schema.json`, `schemas/dunning-event-presentment.schema.json`, `schemas/webhook-outbox-event-presentment.schema.json`, `schemas/issued-invoice.schema.json`, `schemas/issued-invoice-presentment.schema.json`, `schemas/issued-credit-note.schema.json`, `schemas/issued-credit-note-presentment.schema.json`, `schemas/credit-note-application.schema.json`, `schemas/credit-note-application-presentment.schema.json`, `schemas/collection-case-settlement.schema.json`, `schemas/collection-case-settlement-presentment.schema.json`, and `schemas/account-statement-presentment.schema.json`.  They are never IEEE binary floats.
