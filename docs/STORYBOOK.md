# Storybook inventory

Operator presentment stories live in `operator_console/stories/`.  Run them with `cd operator_console && npm install && npm run storybook`.  Storybook is the UI surface for this slice.

Customer copy on every invoice statement: amount due and the next operator action, **Collect or credit**.

Customer copy on every collection case: outstanding and the next operator action. Open the collection case, then collect or credit.

Customer copy on every payment intent: projected amount and the next operator action. Create a projected payment intent, then record the receipt.

## Stories

| Story | Module | Fixtures |
| --- | --- | --- |
| InvoiceStatement | `src/invoice_statement.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| AmountDue | `src/amount_due.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| LineTable | `src/line_table.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| StatusChip | `src/status_chip.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| CollectionCase | `src/collection_case.js` | `open_collection_case.json`, `dunning_collection_case.json`, `settled_collection_case.json` |
| PaymentIntent | `src/payment_intent.js` | `projected_payment_intent.json`, `cancelled_payment_intent.json` |

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

Amounts are canonical decimal strings from `schemas/invoice-draft-presentment.schema.json`, `schemas/collection-case-presentment.schema.json`, and `schemas/payment-intent-presentment.schema.json`.  They are never IEEE binary floats.
