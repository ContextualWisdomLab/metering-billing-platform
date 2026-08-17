# ADR 0009: Payment Receipt From Stored Payment Intents

**Status:** Accepted

## Context

Projected payment intents already persist exact initiation amounts.  Buyers next need a commercial receipt that applies received cash against that intent and reduces collection outstanding.  ISO 20022 keeps payment initiation separate from settlement (International Organization for Standardization, 2026).  PCI DSS scope is reduced by never storing a card PAN (PCI Security Standards Council, 2024).  IEEE (2019) and Cowlishaw (2009) forbid binary floating-point money.  Helland (2012) requires that a replay of the same receipt command return the same stored identity.

This path must not capture via a named provider, store a provider charge identifier, emit an `accounting_journal_proposal`, or post to AIS.  Cash and AR journal proposals remain the next increment.

## Decision

- Expose `PaymentSettlementService.record_payment_receipt` for one tenant, one stored `payment_intent_id`, and one exact `received_amount`.
- Identify a receipt by `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.
- Hash the intent amount, currency, status, and received amount.  Do not include provider object IDs or card PAN.
- Persist append-only `payment_receipt` rows whose status is `applied` only.
- Reduce `collection_case.outstanding_amount` by the applied amount.  Remaining zero marks the case `settled`.  Partial receipts leave residual outstanding and leave the case `open` or `dunning`.
- Expose `PaymentSettlementService.cancel_payment_intent` to flip a projected intent to `cancelled` without writing a receipt or changing outstanding.  Cancel replay is idempotent.  A cancelled intent cannot later receive a receipt.
- Reject a missing or cross-tenant intent without leaking the other tenant's document.
- Reject zero or negative amounts, amounts greater than remaining outstanding, binary floating-point values, and intents that are not `projected`.
- Do not call Stripe/Adyen/Toss or post journals.  Cash journal export is ADR 0010.

## Consequences

- A known projected intent amount can be fully applied; outstanding becomes zero and the case settles.
- A partial receipt leaves residual outstanding for another receipt.
- Tenants cannot see or settle each other's intents.
- Operators next propose a cash journal to AIS, or record another partial receipt.
