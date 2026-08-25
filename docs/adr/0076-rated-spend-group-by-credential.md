# ADR 0076: Rated Spend `group_by=credential`

**Status:** Accepted

## Context

#77 presents already-rated spend for one billing account and half-open window, grouped by `product_code`. #78 adds optional `group_by=project`. The PRD still requires spend inspectable by credential. Usage events already carry optional `credential_reference`. The ledger stores that as `credential_record_id` on exclusive-account usage and keeps the URN on `credential_record`. Rating lines and exclusive draft lines do not.

This repository is not the statutory accounting authority. Credential grouping is presentation of stored exclusive-line amounts keyed by a stored usage credential URN, not a re-rate and not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No second route should exist. This slice adds an optional query value on the existing GET. It does not invent a unit price, include unrated usage, invent a credential or sentinel, open a spend-budget write, add `group_by=principal` or `group_by=cost_center`, invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, collection-status flip, or tenant provisioning.

## Decision

- Keep `GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=&window_ended_at=`.
- Accept optional `group_by=product|project|credential`. Omitting `group_by` or sending `group_by=product` keeps the #77 rows: one per `(currency_code, product_code)` with no `project_reference` or `credential_reference` field.
- `group_by=project` stays the #78 contract: one row per `(currency_code, product_code, project_reference)`.
- `group_by=credential` publishes one row per `(currency_code, product_code, credential_reference)`. `credential_reference` is the stored exclusive-account usage-event credential URN in that window, resolved from the stored `credential_record_id`. Usage without `credential_reference` is omitted from the credential grouping. Mixed credentials omit the run so the read cannot invent a split.
- Unknown `group_by` is HTTP 422 `request_invalid`. Tenant, account, and window fail-closed codes stay the #77 contract.
- Money still comes only from already-stored `rating_run` and exclusive `invoice_draft` lines. Do not re-rate. Do not invent a unit price. Do not include unrated usage.
- Replay is a safe GET and writes no money fact. Do not add a presentment table. Do not call AIS.

## Consequences

- Operators can inspect already-rated product spend by stored credential URN without drafting, issuing, or re-rating.
- Product default stays the #77 contract. Project grouping stays the #78 contract.
- A later slice may add `group_by=principal`. This slice does not.
- #7, #8, #62, #75, #77, and #78 stay unchanged except this additive query value.
