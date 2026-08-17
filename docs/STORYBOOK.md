# Storybook inventory

Operator presentment stories live in `operator_console/stories/`.  Run them with `cd operator_console && npm install && npm run storybook`.  Storybook is the UI surface for this slice.

Customer copy on every invoice statement: amount due and the next operator action, **Collect or credit**.

Customer copy on every collection case: outstanding and the next operator action. Open the collection case, then collect or credit.

Customer copy on every payment intent: projected amount and the next operator action. Create a projected payment intent, then record the receipt.

Customer copy on every payment receipt: received amount and the next operator action. Record the receipt; the cash journal is already validated for AIS to pull.

Customer copy on every credit adjustment: credited amount and the next operator action. Record the credit; AIS pulls the validated journal.

Customer copy on every rate card: unit price and the next operator action. Publish a rate card, then rate a window against that version.

## Stories

| Story | Module | Fixtures |
| --- | --- | --- |
| InvoiceStatement | `src/invoice_statement.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| AmountDue | `src/amount_due.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| LineTable | `src/line_table.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| StatusChip | `src/status_chip.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| CollectionCase | `src/collection_case.js` | `open_collection_case.json`, `dunning_collection_case.json`, `settled_collection_case.json` |
| PaymentIntent | `src/payment_intent.js` | `projected_payment_intent.json`, `cancelled_payment_intent.json` |
| PaymentReceipt | `src/payment_receipt.js` | `applied_full_payment_receipt.json`, `applied_partial_payment_receipt.json` |
| CreditAdjustment | `src/credit_adjustment.js` | `recorded_morning_credit.json`, `recorded_taxed_credit.json` |
| RateCard | `src/rate_card.js` | `published_standard_rate.json`, `published_premium_rate.json` |

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
| `projected_payment_intent.json` | Projected morning intent | `payment_amount` `0.003705`, action `record_receipt` |
| `cancelled_payment_intent.json` | Cancelled intent | `payment_amount` `0.003705`, action `wait` |
| `applied_full_payment_receipt.json` | Full morning receipt | `received_amount` `0.003705`, remaining `0.00`, action `drain_or_wait` |
| `applied_partial_payment_receipt.json` | Partial receipt | `received_amount` `0.001`, remaining `0.002705`, action `record_receipt` |
| `recorded_morning_credit.json` | Full morning credit | `credit_amount` `0.003705`, tax `0`, action `wait` |
| `recorded_taxed_credit.json` | Taxed goodwill credit | `credit_amount` `11.00`, exclusive `10.00`, tax `1.00`, action `wait` |
| `published_standard_rate.json` | Standard token card | `unit_amount` `0.000002`, action `rate_window` |
| `published_premium_rate.json` | Premium token card | `unit_amount` `0.000005`, action `rate_window` |

Amounts are canonical decimal strings from `schemas/invoice-draft-presentment.schema.json`, `schemas/collection-case-presentment.schema.json`, `schemas/payment-intent-presentment.schema.json`, `schemas/payment-receipt-presentment.schema.json`, `schemas/credit-adjustment-presentment.schema.json`, and `schemas/rate-card-presentment.schema.json`.  They are never IEEE binary floats.
