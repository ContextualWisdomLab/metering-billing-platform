# ADR 0074: Rated Spend Presentment by Product

**Status:** Accepted

## Context

Operators can list usage events (#32) and rating runs (#33), and they can open one billing-account commercial statement (#62 / #75 / #76). They still cannot inspect already-rated spend for one billing account inside one half-open window. The PRD requires spend inspectable by product, project, principal, and credential. Usage events already carry `billing_account_reference`, `billing_principal_reference`, `product_code`, and optional project, credential, and cost center.

This repository is not the statutory accounting authority. Rated spend is presentation of stored rating and exclusive draft-line amounts, not a re-rate and not collected revenue (IFRS Foundation, 2024). RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022). Currencies must not be mixed in one sum (IAS 21).

No rated-spend presentment existed. This slice adds a read-only projection grouped by `product_code` only. It does not invent a unit price, include unrated usage, open a spend-budget write, add `group_by=project` or `group_by=credential`, invent a journal, webhook, PSP, VAT register, NTS filing, 연말정산, statutory ID, AIS call, collection-status flip, or tenant provisioning.

## Decision

- Expose `RatedSpendPresentmentService.present_rated_spend(tenant_reference, billing_account_id, time_window)`.
- Expose `GET /v1/billing-accounts/{billing_account_id}/rated-spend?window_started_at=&window_ended_at=`. The window is half-open ISO 8601 and uses the same `TimeWindow` rules as rating. Tenant pin matches #22. Missing tenant is HTTP 422. Missing account is HTTP 404. Cross-tenant account is HTTP 403. An illegal window is HTTP 422.
- Identity is the tenant pin plus `billing_account_id` plus the query window. Replay is a safe GET and writes no money fact.
- Take money only from already-stored `rating_run` rows whose UTC-normalized window equals the query window, and from exclusive `invoice_draft` lines whose usage or draft attribution belongs only to that `billing_account_id`. Mixed-account and lineless drafts are omitted so the read cannot invent a split. When no exclusive draft exists, use stored rating lines already attributed to that account.
- Do not re-rate. Do not invent a unit price. Do not include unrated usage.
- Group this slice by `product_code` only. One row per `(currency_code, product_code)` carries exact `rated_amount` as the stored rated or exclusive draft line amount, published as an exact Decimal string. Mixed product codes on exclusive-account usage in that window omit the run so the read cannot invent a product split.
- Do not add `group_by`. Do not add a presentment table. Do not call AIS.

## Consequences

- Operators can inspect already-rated product spend for one billing account and window without drafting, issuing, or re-rating.
- A later slice may add `group_by=project`. This slice does not.
- #7, #8, #62, #75, and #76 stay unchanged.
