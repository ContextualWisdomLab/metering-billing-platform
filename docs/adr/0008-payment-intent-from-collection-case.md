# ADR 0008: Payment Intent From Stored Collection Cases

**Status:** Accepted

## Context

Collection cases already persist exact outstanding.  Buyers next need a provider-neutral payment initiation record they can later bind to a processor.  ISO 20022 payment initiation is a later provider projection, not an internal capture (International Organization for Standardization, 2026).  PCI DSS scope is reduced by never storing a card PAN (PCI Security Standards Council, 2024).  Helland (2012) requires that a replay of the same project command return the same stored identity.

This path must not capture money, settle, post a journal, or call a named payment provider.

## Decision

- Expose `PaymentIntentService.project_payment_intent` for one tenant and one stored `collection_case_id`.
- Identify an intent by `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.
- Hash the case outstanding, currency, and stored status snapshot.  Do not include provider object IDs.
- Persist append-only `payment_intent` rows whose amount equals the case outstanding.
- Keep `payment_intent_status` in `projected`, `cancelled`, or `rejected`.  This path writes `projected`.  Never `captured`, `settled`, or `posted`.
- Reject a missing or cross-tenant case without leaking the other tenant's document.
- Reject zero or negative amounts and binary floating-point values.
- Do not change collection-case outstanding, issue a statutory invoice, or post to AIS.

## Consequences

- Known collection-case outstanding reproduces one exact projected amount.
- Tenants cannot see or project each other's cases.
- Operators next bind a payment-provider projection or cancel the intent.  Capture remains a later increment.
