# ADR 0059: Account Statement Presentment

**Status:** Accepted

## Context

Leftover cash now parks, applies, refunds, and books those three journals. Operators still have no single rollup of what a billing account owes, has paid, has credited, or has parked. Closest facts are collection aging (#53), issued invoices (#41), collection cases, credit-note applications, write-offs, unapplied cash, and leftover refunds.

This repository is not the statutory accounting authority. A statement is presentation of stored commercial facts, not a posted trial balance (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No account-statement presentment existed. This slice adds a read-only projection. It does not invent a journal, webhook, PSP, void, dispute, settlement rewrite, AIS call, statutory numbering, or new money fact.

## Decision

- Expose `AccountStatementPresentmentService.present_account_statement(tenant_reference, billing_account_id)`.
- Expose `GET /v1/billing-accounts/{billing_account_id}/statement`. Tenant pin matches #22. Missing tenant is HTTP 422. Missing account is HTTP 404. Cross-tenant account is HTTP 403.
- Attribute money only through invoice-draft lines that belong exclusively to the requested billing account. Drafts with no lines, or lines for more than one account, are omitted so the read cannot invent a split.
- Group totals by `currency_code`. Each currency row carries exact inclusive `issued_invoice_total`, `open_collection_remaining`, `applied_credit_total`, `write_off_total`, `parked_unapplied_cash`, and `refunded_unapplied_cash`.
- Open remaining includes stored `open` or `dunning` cases whose remaining is a positive exact decimal. Settled cases and exact-zero remaining are omitted.
- Parked leftover is unused `unapplied_cash` that has no apply and no refund row. Refunded leftover is stored `unapplied_cash_refund` amount.
- Do not add a presentment table. Do not call AIS.

## Consequences

- Operators open one billing-account statement, then collect, credit, park, apply, or refund from existing commands.
- Storybook can consume this JSON later. This slice does not add a production SPA.
- #13, #15, #29, #51, #53, #54, #55, #57, #58, #59, #60, and #61 stay unchanged.
