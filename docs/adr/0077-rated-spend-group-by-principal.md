# ADR 0077: Rated Spend `group_by=principal`

**Status:** Accepted

## Context

#77 presents already-rated spend for one billing account and half-open window, grouped by `product_code`. #78 adds optional `group_by=project`. #79 adds optional `group_by=credential`. The PRD still requires spend inspectable by principal. Usage events already require `billing_principal_reference`. The ledger stores that as required `billing_principal_id` on exclusive-account usage and keeps the URN on `BillingPrincipal`. Rating lines and exclusive draft lines do not.

This repository is not the statutory accounting authority. Principal grouping is presentation of stored exclusive-line amounts keyed by a stored usage principal URN, not a re-rate and not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No second route should exist. This slice adds an optional query value on the existing GET. It does not invent a unit price, include unrated usage, invent a principal or sentinel, open a spend-budget write, add `group_by=cost_center`, invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, collection-status flip, or tenant provisioning.

## Decision

- Keep `GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=&window_ended_at=`.
- Accept optional `group_by=product|project|credential|principal`. Omitting `group_by` or sending `group_by=product` keeps the #77 rows: one per `(currency_code, product_code)` with no `project_reference`, `credential_reference`, or `billing_principal_reference` field.
- `group_by=project` stays the #78 contract. `group_by=credential` stays the #79 contract.
- `group_by=principal` publishes one row per `(currency_code, product_code, billing_principal_reference)`. `billing_principal_reference` is the stored exclusive-account usage-event principal URN in that window, resolved from the stored `billing_principal_id`. The usage-event contract already requires a principal, so this grouping does not drop events for missing principals. Mixed principals omit the run so the read cannot invent a split. An unresolved `billing_principal_id` omits the run.
- Unknown `group_by` is HTTP 422 `request_invalid`. Tenant, account, and window fail-closed codes stay the #77 contract.
- Money still comes only from already-stored `rating_run` and exclusive `invoice_draft` lines. Do not re-rate. Do not invent a unit price. Do not include unrated usage.
- Replay is a safe GET and writes no money fact. Do not add a presentment table. Do not call AIS.

## Consequences

- Operators can inspect already-rated product spend by stored principal URN without drafting, issuing, or re-rating.
- Product default stays the #77 contract. Project grouping stays the #78 contract. Credential grouping stays the #79 contract.
- A later slice may add `group_by=cost_center`. This slice does not.
- #7, #8, #62, #75, #77, #78, and #79 stay unchanged except this additive query value.
