# ADR 0012: Journal Proposal Query Without Mutation

**Status:** Accepted

## Context

HTTP accept (#14 / ADR 0011) is write-only.  AIS is building a pull accept surface and cannot consume `accounting_journal_proposal` documents without a query.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  ISO 20022 keeps initiation (`pain.001`) separate from reporting and cash evidence (`camt`) (International Organization for Standardization, 2026).  Collection resources page with a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  A pull must not mark a proposal `exported`, `posted`, or consumed.  AIS owns `posting_receipt` and will not ask Billing to flip status after pull.

Cash proposals already live in `journal_proposal`.  A second table or a second list shape would invent a new entity and break the published contract.

## Decision

- Expose `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}` on the existing stdlib WSGI app.
- Require a tenant on every read.  Accept optional `X-CWL-Tenant-Reference` as the tenant pin.  Body or query `tenant_reference` still works when the header is absent.  If both are present they must be identical; a mismatch is HTTP 422.  Do not require the header.  Optional filters are `proposal_status` (`draft|validated|exported|rejected` only), inclusive `proposed_after`, and a bounded `cursor` / `page_limit`.
- Order by `proposed_at` then `proposal_id`.  List items are the published `accounting_journal_proposal` contract from `as_contract_dict`.  Do not invent a second item shape.
- Return HTTP 200 for a successful read, HTTP 422 for a missing tenant or illegal filter, and HTTP 404 for an unknown route or an unknown/cross-tenant proposal.  Do not invent 401/403 in this slice.
- Reuse `journal_proposal` persistence.  Cash and AR proposals share that store and therefore appear in the same list.  Do not add `GET /v1/cash-journal-proposals`.
- Never mutate `proposal_status`.  Never emit `posted`.  Never emit statutory account IDs; AIS maps semantic roles such as `cash_receipt` to its own chart.

## Consequences

- AIS pulls validated proposals.  Billing does not flip status after that pull.
- Tenants cannot list or fetch each other's proposals.
- A later persistent ledger can replace `MemoryUsageLedger` without changing routes, cursors, or the published item contract.
