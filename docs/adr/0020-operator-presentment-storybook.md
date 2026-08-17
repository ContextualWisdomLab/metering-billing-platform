# ADR 0020: Operator Presentment Storybook

**Status:** Accepted

## Context

#21 publishes a tenant-scoped invoice-draft statement over HTTP.  Operators still cannot see that statement.  IFRS 15 treats the document as presentation of consideration (exclusive, tax, credits, and amount due), not as earned revenue (IFRS Foundation, 2024).  ADR 0018 deferred Storybook and Figma.  This slice adds the operator console without a production SPA, login wall, Stripe, or AIS call.

Figma is optional.  A missing Figma session must not block the presentment console.

## Decision

- Add `operator_console/` as a standalone npm package that is also importable (`@cwl/operator-console`).  Do not replace `metering_billing`.
- Tokenize color, spacing, type, and radius as CSS variables plus `tokens/design_tokens.json`.  Repeated objects (`AmountDue`, `LineTable`, `StatusChip`, tenant pin) are modules, not one-off CSS.
- Ship Storybook stories for `InvoiceStatement`, `AmountDue`, `LineTable`, and `StatusChip` using realistic fixtures: taxed plus partial credit, untaxed morning usage, and settled.
- Render exact-decimal strings from the #21 schema.  Float money fails closed.
- Keep Storybook as the UI surface.  Do not add a production webpack SPA deploy.
- Do not change HTTP, tax, credit, or credential contracts.

## Consequences

- Operators open the draft statement in Storybook and see amount due plus the next action: collect or credit.
- Python remains the commercial authority.  The console only presents stored statement JSON.
- A later production host can import the same modules without rewriting tokens or fixtures.
