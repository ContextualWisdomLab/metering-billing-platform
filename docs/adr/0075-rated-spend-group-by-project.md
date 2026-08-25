# ADR 0075: Rated Spend `group_by=project`

**Status:** Accepted

## Context

#77 presents already-rated spend for one billing account and half-open window, grouped by `product_code`. The PRD still requires spend inspectable by project. Usage events already carry optional `project_reference`. Rating lines and exclusive draft lines do not.

This repository is not the statutory accounting authority. Project grouping is presentation of stored exclusive-line amounts keyed by a stored usage URN, not a re-rate and not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No second route should exist. This slice adds an optional query parameter on the existing GET. It does not invent a unit price, include unrated usage, invent a project or sentinel, open a spend-budget write, add `group_by=credential` or `group_by=principal`, invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, collection-status flip, or tenant provisioning.

## Decision

- Keep `GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=&window_ended_at=`.
- Accept optional `group_by=product|project`. Omitting `group_by` or sending `group_by=product` keeps the #77 rows: one per `(currency_code, product_code)` with no `project_reference` field.
- `group_by=project` publishes one row per `(currency_code, product_code, project_reference)`. `project_reference` is the stored exclusive-account usage URN in that window. Usage without `project_reference` is omitted from the project grouping. Mixed projects omit the run so the read cannot invent a split.
- Unknown `group_by` is HTTP 422 `request_invalid`. Tenant, account, and window fail-closed codes stay the #77 contract.
- Money still comes only from already-stored `rating_run` and exclusive `invoice_draft` lines. Do not re-rate. Do not invent a unit price. Do not include unrated usage.
- Replay is a safe GET and writes no money fact. Do not add a presentment table. Do not call AIS.

## Consequences

- Operators can inspect already-rated product spend by stored project URN without drafting, issuing, or re-rating.
- Product default stays the #77 contract.
- A later slice may add `group_by=credential`. This slice does not.
- #7, #8, #62, #75, and #77 stay unchanged except this additive query parameter.
