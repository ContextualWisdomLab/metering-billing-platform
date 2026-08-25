# ADR 0017: Tax-Payable Unwind on Credit Adjustment

**Status:** Accepted

## Context

#19 / ADR 0016 assesses tax on an invoice draft and credits semantic `tax_payable` on the commercial journal.  #17 / ADR 0014 still records credits as a two-line revenue debit / AR credit against inclusive remaining.  A taxed credit that does not debit `tax_payable` leaves AIS books drifted from the original three-line proposal (IFRS Foundation, 2024; IFRS Foundation, n.d.).

ISO 4217 minor units remain the rounding grid (International Organization for Standardization, 2015).  IFRS 15 treats a later price concession as variable consideration, not a posted statutory reversal (IFRS Foundation, 2024).  This repository still does not post, call AIS, or emit statutory account IDs.

## Decision

- Keep `CreditAdjustmentService.record_credit_adjustment` as the buyer path.  Collection outstanding still reduces by the inclusive `credit_amount`.
- When a `tax_assessment` exists, split the inclusive credit proportionally:
  `credit_tax_amount = round_half_even(credit_amount * tax_amount / tax_inclusive_amount)` to the currency minor units.  `credit_tax_exclusive` is the remainder so the parts sum to `credit_amount`.
- A full credit of `tax_inclusive_amount` therefore reconstructs the original exclusive and tax amounts exactly.
- Persist `tax_exclusive_amount` and `tax_amount` on `credit_adjustment`.  Untaxed credits store exclusive equal to `credit_amount` and tax `0`.
- Emit one balanced validated proposal (never posted):
  - taxed with positive tax: debit `usage_revenue`, debit `tax_payable`, credit `accounts_receivable`
  - untaxed or zero tax: debit `usage_revenue`, credit `accounts_receivable`
- Keep the idempotency key `{tenant}:credit_adjustment:{credit_adjustment_id}:{source_payload_hash}:v{version}`.  The credit and journal hashes include the tax split when the draft is taxed.
- Expose `tax_exclusive_amount` and `tax_amount` on the existing credit HTTP contract.  Routes stay `POST /v1/credit-adjustments` and `GET /v1/credit-adjustments/{credit_adjustment_id}`.
- Fail closed on missing assessment fields, a split that does not sum, unknown currency exponents, floats, and cross-tenant access (`tax_split_invalid` or the existing tenant/draft reasons).

## Consequences

- Operators record the credit; AIS pulls the validated three-line unwind.
- AIS already maps `usage_revenue` and `accounts_receivable`; it must also map `tax_payable` from #19.
- Untaxed credits remain the #17 two-line journal.
- No reversing proposal is emitted for AIS `POST /journal-reversals`.  No operator UI is added.
