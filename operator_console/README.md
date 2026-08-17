# Operator console

Storybook surface for the #21 invoice-draft presentment contract.  This package is importable as `@cwl/operator-console` and does not replace `metering_billing`.

There is no login wall, no Stripe, and no AIS call.  Storybook is the UI for this slice.  Do not add a production webpack SPA deploy here.

## Customer copy

Amount due and the next operator action: collect or credit.

## Run Storybook

```bash
cd operator_console
npm install
npm run lint
npm test
npm run storybook
```

Stories live in `stories/`.  Fixtures live in `fixtures/` and must stay exact-decimal strings from `schemas/invoice-draft-presentment.schema.json`.
