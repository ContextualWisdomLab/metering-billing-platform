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

Customer copy on every spend budget: remaining, over, utilization, and the next operator action. Publish a commercial spend budget, then wait.

Customer copy on every rated-spend presentment: already-rated amount and the next operator action. Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice.

Customer copy on every account budget status: remaining, over, utilization, and the next operator action. Open the account budget status, then wait.

Customer copy on every spend-budget over observation: over, budget, utilization, and the next operator action. Observe the spend budget over signal, then wait.

Customer copy on every spend-budget approaching observation: remaining, budget, utilization, and the next operator action. Observe the spend budget approaching signal, then wait.

Customer copy on every collection dispute: remaining and the next operator action. Hold the disputed case, then wait. Release the hold, then collect or dunn.

Customer copy on every leftover / unapplied cash: leftover amount and the next operator action. Park leftover remittance, then wait. Apply parked leftover, then collect the residual. Refund unused parked leftover, then wait.

Customer copy on every unused issued-credit-note void: voided amount and the next operator action. Void an unused issued credit note, then wait.

Customer copy on every unused issued-invoice void: inclusive voided amount and the next operator action. Void an unused issued invoice, then wait.

Customer copy on every collection write-off: leftover remaining written off, exact-zero remaining, and the next operator action. Write off leftover remaining, then settle.

Customer copy on every journal proposal: validated cash, invoice-draft, leftover, leftover-apply, or leftover-refund amount and the next operator action. Let AIS pull the validated journal.

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
| CollectionCaseSettlement | `src/collection_case_settlement.js` | `settled_morning_zero.json`, `settled_leftover_write_off_zero.json` |
| AccountStatement | `src/account_statement.js` | `settled_account_statement.json`, `voided_account_statement.json` |
| SpendBudget | `src/spend_budget.js` | `published_under_budget.json`, `published_at_budget.json`, `published_over_budget.json` |
| RatedSpend | `src/rated_spend.js` | `rated_spend_morning_product.json`, `rated_spend_morning_project.json` |
| BudgetStatus | `src/budget_status.js` | `account_budget_status_under_over.json`, `account_budget_status_next_cursor.json` |
| SpendBudgetOver | `src/spend_budget_over.js` | `accepted_over_signal.json`, `accepted_under_signal.json`, `duplicate_replay_over_signal.json`, `pending_spend_budget_over.json` |
| SpendBudgetApproaching | `src/spend_budget_approaching.js` | `accepted_at_signal.json`, `accepted_under_approaching_signal.json`, `duplicate_replay_approaching_signal.json`, `pending_spend_budget_approaching.json` |
| CollectionDispute | `src/collection_dispute.js` | `held_morning_collection_dispute.json`, `released_morning_collection_dispute.json` |
| UnappliedCash | `src/unapplied_cash.js` | `parked_morning_unapplied_cash.json`, `applied_morning_unapplied_cash.json`, `refunded_morning_unapplied_cash.json` |
| IssuedCreditNoteVoid | `src/issued_credit_note_void.js` | `voided_unused_issued_credit_note.json` |
| IssuedInvoiceVoid | `src/issued_invoice_void.js` | `voided_unused_issued_invoice.json` |
| CollectionWriteOff | `src/collection_write_off.js` | `recorded_leftover_collection_write_off.json` |
| JournalProposal | `src/journal_proposal.js` | `validated_morning_cash_journal.json`, `validated_morning_invoice_draft_journal.json`, `validated_taxed_invoice_draft_journal.json`, `validated_morning_leftover_journal.json`, `validated_morning_leftover_apply_journal.json`, `validated_morning_leftover_refund_journal.json` |

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
| `settled_leftover_write_off_zero.json` | Settle-when-zero of the leftover write-off case | remaining `0`, case `settled`, action `wait` |
| `settled_account_statement.json` | Settled billing-account statement | remaining `0.00`, applied `110.00`, voids `0` |
| `voided_account_statement.json` | Unused invoice and credit voids | issued `110.00`, voided invoice `110.00`, voided credit `11.00` |
| `published_under_budget.json` | Published morning budget under rated spend | budget `100.00`, rated `0.003705`, remaining `99.996295`, over `0`, utilization `under` |
| `published_at_budget.json` | Published morning budget at rated spend | budget `0.003705`, rated `0.003705`, remaining `0`, over `0`, utilization `at` |
| `published_over_budget.json` | Published morning budget over rated spend | budget `0.001`, rated `0.003705`, remaining `0`, over `0.002705`, utilization `over` |
| `rated_spend_morning_product.json` | Product-grouped morning rated spend | `rated_amount` `0.003705`, product `contextual_orchestrator` |
| `rated_spend_morning_project.json` | Project-grouped morning rated spend | `rated_amount` `0.003705`, project `urn:cwl:tenant_001:project:metering` |
| `account_budget_status_under_over.json` | Same-tenant account published budgets | under remaining `99.996295`, at remaining `0`, over `0.002705`, `next_cursor` null |
| `account_budget_status_next_cursor.json` | Keyset page of the same account | under + at, `next_cursor` `2026-08-18T15:00:00Z\|019d7b92-1aa0-7a7f-b61c-962c0f4bfe02` |
| `accepted_over_signal.json` | First-over accepted enqueue | over `0.002705`, budget `0.001`, utilization `over`, outcome `accepted` |
| `accepted_under_signal.json` | Same-tenant under observation | over `0`, budget `100.00`, utilization `under`, zero over-signal rows |
| `duplicate_replay_over_signal.json` | Replay of the same over source | over `0.002705`, outcome `duplicate_replay`, outbox stays first-over-wins |
| `pending_spend_budget_over.json` | First-over #24 outbox presentment | type `spend_budget.over`, source first-over budget, delivery `pending` |
| `accepted_at_signal.json` | First-at accepted enqueue | remaining `0`, budget `0.003705`, utilization `at`, outcome `accepted` |
| `accepted_under_approaching_signal.json` | Same-tenant under observation | remaining `99.996295`, budget `100.00`, utilization `under`, zero approaching-signal rows |
| `duplicate_replay_approaching_signal.json` | Replay of the same at source | remaining `0`, outcome `duplicate_replay`, outbox stays first-at-wins |
| `pending_spend_budget_approaching.json` | First-at #24 outbox presentment | type `spend_budget.approaching`, source first-at budget, delivery `pending` |
| `held_morning_collection_dispute.json` | Held morning collection dispute | remaining `0.003705`, status `held`, case `disputed`, action `wait` |
| `released_morning_collection_dispute.json` | Released/fail-close of the same hold | remaining `0.003705`, status `released`, case `open`, action `wait` |
| `parked_morning_unapplied_cash.json` | Parked morning leftover | leftover `0.001`, received `0.003705`, status `parked`, action `wait` |
| `applied_morning_unapplied_cash.json` | Leftover-apply to another open case | applied `0.001`, remaining `19.999`, status `applied`, action `collect` |
| `refunded_morning_unapplied_cash.json` | Leftover-refund of the parked leftover | refund `0.001`, leftover stays `parked`, action `wait` |
| `voided_unused_issued_credit_note.json` | Unused taxed issued-credit-note void | voided `11.00`, status `recorded`, action `wait` |
| `voided_unused_issued_invoice.json` | Unused taxed issued-invoice void | voided `110.00`, status `recorded`, action `wait` |
| `recorded_leftover_collection_write_off.json` | Recorded leftover remaining write-off | leftover `0.001`, remaining `0`, case `open`, action `settle` |
| `validated_morning_cash_journal.json` | Validated morning cash journal | cash debit `0.003705`, status `validated`, action `wait` |
| `validated_morning_invoice_draft_journal.json` | Validated morning invoice-draft journal | AR debit `0.003705`, revenue credit `0.003705`, status `validated`, action `wait` |
| `validated_taxed_invoice_draft_journal.json` | Validated taxed invoice-draft journal | AR debit `110.00`, revenue credit `100.00`, tax payable `10.00`, status `validated`, action `wait` |
| `validated_morning_leftover_journal.json` | Validated leftover journal | cash debit `0.001`, unapplied-cash credit `0.001`, leftover stays `parked`, status `validated`, action `wait` |
| `validated_morning_leftover_apply_journal.json` | Validated leftover-apply journal | unapplied-cash debit `0.001`, AR credit `0.001`, leftover-apply remaining stays `19.999`, status `validated`, action `wait` |
| `validated_morning_leftover_refund_journal.json` | Validated leftover-refund journal | unapplied-cash debit `0.001`, cash credit `0.001`, leftover stays `parked`, leftover-apply remaining stays `19.999`, status `validated`, action `wait` |

Amounts are canonical decimal strings from `schemas/invoice-draft-presentment.schema.json`, `schemas/collection-case-presentment.schema.json`, `schemas/payment-intent-presentment.schema.json`, `schemas/payment-receipt-presentment.schema.json`, `schemas/credit-adjustment-presentment.schema.json`, `schemas/rate-card-presentment.schema.json`, `schemas/usage-event-presentment.schema.json`, `schemas/rating-run-presentment.schema.json`, `schemas/tax-assessment-presentment.schema.json`, `schemas/posting-receipt-observation-presentment.schema.json`, `schemas/webhook-delivery-presentment.schema.json`, `schemas/tenant-api-credential-presentment.schema.json`, `schemas/webhook-subscription-presentment.schema.json`, `schemas/dunning-event-presentment.schema.json`, `schemas/webhook-outbox-event-presentment.schema.json`, `schemas/issued-invoice.schema.json`, `schemas/issued-invoice-presentment.schema.json`, `schemas/issued-credit-note.schema.json`, `schemas/issued-credit-note-presentment.schema.json`, `schemas/credit-note-application.schema.json`, `schemas/credit-note-application-presentment.schema.json`, `schemas/collection-case-settlement.schema.json`, `schemas/collection-case-settlement-presentment.schema.json`, `schemas/account-statement-presentment.schema.json`, `schemas/spend-budget-presentment.schema.json`, `schemas/spend-budget-evaluation-presentment.schema.json`, `schemas/rated-spend-presentment.schema.json`, `schemas/billing-account-budget-status-presentment.schema.json`, `schemas/spend-budget-over-signal.schema.json`, `schemas/spend-budget-approaching-signal.schema.json`, `schemas/collection-dispute-presentment.schema.json`, `schemas/collection-dispute-release-presentment.schema.json`, `schemas/unapplied-cash-presentment.schema.json`, `schemas/unapplied-cash-application-presentment.schema.json`, `schemas/unapplied-cash-refund-presentment.schema.json`, `schemas/issued-credit-note-void-presentment.schema.json`, `schemas/issued-invoice-void-presentment.schema.json`, `schemas/collection-write-off-presentment.schema.json`, and `schemas/accounting-journal-proposal.schema.json`.  They are never IEEE binary floats.  Budget-status fixtures keep the GET envelope `{budget_statuses, next_cursor}` and pin `X-CWL-Tenant-Reference` to commercial `tenant_account` `urn:cwl:tenant_001`.  Spend-budget-over observation fixtures keep the existing over-signal write and pin the same tenant.  The first-over outbox fixture keeps the existing webhook-outbox presentment envelope (`event_type_code` `spend_budget.over`).  Spend-budget-approaching observation fixtures keep the existing approaching-signal write and pin the same tenant.  The first-at outbox fixture keeps the existing webhook-outbox presentment envelope (`event_type_code` `spend_budget.approaching`).  Collection-dispute fixtures keep the existing hold and release presentment envelopes and pin the same tenant.  Leftover / unapplied-cash fixtures keep the existing parked, leftover-apply, and leftover-refund presentment envelopes and pin the same tenant.  Unused issued-credit-note-void fixtures keep the existing unused-void presentment envelope and pin the same tenant.  Unused issued-invoice-void fixtures keep the existing unused issued-invoice-void presentment envelope and pin the same tenant.  Collection-write-off fixtures keep the existing leftover-remaining write-off presentment envelope and pin the same tenant.  Collection-case-settlement fixtures keep the existing settle-when-zero presentment envelope.  The leftover write-off settle fixture pins the same tenant and the same `collection_case_id` as the leftover remaining write-off.  Journal-proposal fixtures keep the existing accounting-journal-proposal GET envelope and pin the same tenant.  The morning cash-journal fixture pins the same payment receipt as the full PaymentReceipt fixture and the same `proposal_id` as the pending `journal_proposal.validated` outbox `source_id`.  The morning invoice-draft journal fixture pins the same `invoice_draft_id` as the untaxed InvoiceStatement fixture.  The taxed invoice-draft journal fixture pins the same `invoice_draft_id` as the taxed IssuedInvoice and morning VAT TaxAssessment fixtures.  The leftover-journal fixture pins the same `unapplied_cash_id` as the parked leftover UnappliedCash fixture.  The leftover-apply journal fixture pins the same `unapplied_cash_application_id` as the leftover-apply UnappliedCash fixture.  The leftover-refund journal fixture pins the same `unapplied_cash_refund_id` as the leftover-refund UnappliedCash fixture.  Existing cash, invoice-draft, leftover, and leftover-apply JournalProposal stay those presentments.  Existing leftover / unapplied-cash Storybook stays the leftover-row presentment.  Existing leftover-apply UnappliedCash Storybook stays the leftover-apply row presentment.  Existing leftover-refund UnappliedCash Storybook stays the leftover-refund row presentment.
