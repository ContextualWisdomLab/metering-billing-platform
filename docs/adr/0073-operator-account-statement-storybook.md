# ADR 0073: Operator Account-Statement Storybook

**Status:** Accepted

## Context

#62 publishes one billing-account commercial statement over `GET /v1/billing-accounts/{billing_account_id}/statement`. #75 adds exact inclusive `voided_invoice_total` and `voided_credit_total` on each currency row. `operator_console` already has invoice-draft, collection-aging, and settlement stories, plus a settled invoice-draft fixture, but it has no tokenized AccountStatement module.

This repository is not the statutory accounting authority. A statement is presentation of stored commercial facts, not a posted trial balance (IFRS Foundation, 2024). IAS 21 requires source currency to stay unmixed (IFRS Foundation, 2024). Storybook is the operator UI surface; this slice does not add a production SPA, login wall, Stripe, or AIS call.

#62 and #75 stay unchanged as HTTP and totals contracts.

## Decision

- Add a tokenized `AccountStatement` module that reuses `AmountDue`, `StatusChip`, the tenant pin, and existing design tokens.
- Ship Storybook stories for the settled account-statement fixture and a same-currency fixture whose inclusive `voided_invoice_total` and `voided_credit_total` are exact non-zero strings.
- Keep amounts as exact-decimal strings. Float money fails closed.
- Do not invent a new money widget, change statement totals or attribution, or add an HTTP write.
- Do not invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, collection-status flip, or tenant provisioning.

## Consequences

- Operators can open a billing-account statement in Storybook and see remaining, unused voids, and the next action: collect, credit, park, apply, or refund.
- Python remains the commercial authority. The console only presents stored #62/#75 JSON.
- #62 and #75 stay immutable.
