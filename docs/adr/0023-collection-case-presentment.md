# ADR 0023: Collection Case Presentment HTTP

**Status:** Accepted

## Context

#21/#23 present a stored invoice draft so operators can collect or credit.  After that statement, operators still cannot read the stored `collection_case` and dunning rows as a statement.  IFRS 15 treats remaining consideration as presentation, not as collected revenue (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).  List pages use a deterministic cursor rather than a mutable offset (Google, 2024).

This repository is not the statutory accounting authority.  Presentment must project existing collection and dunning rows.  It must not capture cards, call AIS, flip `proposal_status`, add a scheduler, or start a production SPA.

## Decision

- Expose `CollectionCasePresentmentService.present_collection_case(tenant_reference, collection_case_id)`.
- Project one tenant-scoped statement: `collection_case_id`, `tenant_reference`, `invoice_draft_id`, `currency_code`, `collection_outstanding`, `collection_case_status` (`open` / `dunning` / `settled`), last and next dunning notice codes when those rows exist, `dunning_events`, and `next_operator_action` (`collect`, `credit`, or `wait`).
- Derive status with the #10 rule: settled wins; otherwise dunning events project `dunning`; otherwise the stored status remains `open`.
- Derive next action from stored facts only: settled or zero outstanding waits; an open case with an accepted credit still has adjustable consideration so the next action is credit; otherwise collect.
- Next dunning notice is `first_notice` when none exist, `overdue_notice` after `first_notice`, and omitted after `overdue_notice` or when settled.
- Expose `GET /v1/collection-cases/{collection_case_id}` on the existing WSGI app.  Tenant pin matches #22.  Same-tenant hit is HTTP 200.  Cross-tenant or unknown is HTTP 404 with no leak.  Missing tenant is HTTP 422.
- Expose `GET /v1/collection-cases` as `{collection_cases, next_cursor}` summaries ordered by `opened_at` then `collection_case_id`.  Never `items` or `cursor`.  `page_limit` defaults to 50 and maxes at 100.
- Do not add a presentment table, payment capture, AIS call, or scheduler.  Payment intents stay projected-only.

## Consequences

- Operators open the collection case, then collect or credit.
- Storybook can consume this JSON.  This slice does not add a production SPA, login wall, or Figma-only work.
- Collection outstanding remains a separate commercial fact from invoice `amount_due`.
