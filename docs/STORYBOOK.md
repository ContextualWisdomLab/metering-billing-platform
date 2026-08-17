# Storybook inventory

Operator presentment stories live in `operator_console/stories/`.  Run them with `cd operator_console && npm install && npm run storybook`.  Storybook is the UI surface for this slice.

Customer copy on every statement: amount due and the next operator action, **Collect or credit**.

## Stories

| Story | Module | Fixtures |
| --- | --- | --- |
| InvoiceStatement | `src/invoice_statement.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| AmountDue | `src/amount_due.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| LineTable | `src/line_table.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |
| StatusChip | `src/status_chip.js` | `taxed_partial_credit.json`, `untaxed_morning.json`, `settled_statement.json` |

The tenant pin is a tokenized module composed into `InvoiceStatement`.  It is not a one-off style.

## Fixtures

| File | Meaning | Exact strings |
| --- | --- | --- |
| `taxed_partial_credit.json` | VAT assessed, goodwill credit 11.00 | `amount_due` `99.00`, inclusive `110.00` |
| `untaxed_morning.json` | Known morning tokens, no tax | `amount_due` `0.003705`, tax `0` |
| `settled_statement.json` | Full inclusive credit | `amount_due` `0.00` |

Amounts are canonical decimal strings from `schemas/invoice-draft-presentment.schema.json`.  They are never IEEE binary floats.
