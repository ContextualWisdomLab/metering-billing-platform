# ADR 0078: Rated Spend `group_by=cost_center`

**Status:** Accepted

## Context

#77 presents already-rated spend for one billing account and half-open window, grouped by `product_code`. #78 adds optional `group_by=project`. #79 adds optional `group_by=credential`. #80 adds optional `group_by=principal`. The PRD still requires spend inspectable by cost center. Usage events already carry optional `cost_center_reference`. `StoredUsageEvent.cost_center_reference` is that optional URN. Rating lines and exclusive draft lines do not.

This repository is not the statutory accounting authority. Cost-center grouping is presentation of stored exclusive-line amounts keyed by a stored usage URN, not a re-rate and not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No second route should exist. This slice adds an optional query value on the existing GET. It does not invent a unit price, include unrated usage, invent a cost center or sentinel, open a spend-budget write, invent a cost-center catalog table, invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, collection-status flip, or tenant provisioning.

## Decision

- Keep `GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=&window_ended_at=`.
- Accept optional `group_by=product|project|credential|principal|cost_center`. Omitting `group_by` or sending `group_by=product` keeps the #77 rows: one per `(currency_code, product_code)` with no `project_reference`, `credential_reference`, `billing_principal_reference`, or `cost_center_reference` field.
- `group_by=project` stays the #78 contract. `group_by=credential` stays the #79 contract. `group_by=principal` stays the #80 contract.
- `group_by=cost_center` publishes one row per `(currency_code, product_code, cost_center_reference)`. `cost_center_reference` is the stored exclusive-account usage-event cost-center URN in that window. Usage without `cost_center_reference` is omitted from the cost-center grouping. Mixed cost centers omit the run so the read cannot invent a split.
- Unknown `group_by` is HTTP 422 `request_invalid`. Tenant, account, and window fail-closed codes stay the #77 contract.
- Money still comes only from already-stored `rating_run` and exclusive `invoice_draft` lines. Do not re-rate. Do not invent a unit price. Do not include unrated usage.
- Replay is a safe GET and writes no money fact. Do not add a presentment table. Do not add a cost-center catalog. Do not call AIS.

## Consequences

- Operators can inspect already-rated product spend by stored cost-center URN without drafting, issuing, or re-rating.
- Product default stays the #77 contract. Project, credential, and principal grouping stay #78 / #79 / #80.
- A later slice may open a spend-budget write. This slice does not.
- #7, #8, #62, #75, #77, #78, #79, and #80 stay unchanged except this additive query value.
