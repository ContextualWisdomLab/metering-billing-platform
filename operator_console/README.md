# Operator console

Storybook surface for the #21 invoice-draft, #41 issued-invoice, #62/#75 account-statement, #77–#81 rated-spend, #82/#93 spend-budget, #97 budget-status, and #101 spend-budget-over presentment contracts.  This package is importable as `@cwl/operator-console` and does not replace `metering_billing`.

There is no login wall, no Stripe, and no AIS call.  Storybook is the UI for this slice.  Do not add a production webpack SPA deploy here.

## Customer copy

Amount due and the next operator action: collect or credit.  Issued invoices use the same action after issue.  Account statements show remaining and the next action: collect, credit, park, apply, or refund.  Rated spend shows already-rated product or project amounts; the next action is draft an invoice.  Spend budgets show remaining, over, and utilization; the next action is wait.  Account budget status lists those published evaluations for one billing account; the next action is wait.  Spend-budget over observations show exact over and budget amounts plus utilization; the next action is wait.

## Run Storybook

```bash
cd operator_console
npm install
npm run lint
npm test
npm run storybook
```

Stories live in `stories/`.  Fixtures live in `fixtures/` and must stay exact-decimal strings from `schemas/invoice-draft-presentment.schema.json`, `schemas/account-statement-presentment.schema.json`, `schemas/rated-spend-presentment.schema.json`, `schemas/spend-budget-evaluation-presentment.schema.json`, `schemas/billing-account-budget-status-presentment.schema.json`, `schemas/spend-budget-over-signal.schema.json`, and `schemas/webhook-outbox-event-presentment.schema.json`.
