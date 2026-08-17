# ADR 0002: Separate Billing from Statutory Accounting

**Status:** Accepted

## Context

Operational billing facts and legal accounting records have different authorities, correction rules, periods, policies, and audit obligations. Combining them would couple product pricing to accounting policy, make provider migration dangerous, and allow an operational webhook to affect legal books.

IFRS 15 remains the revenue-from-contracts standard. The 2024 post-implementation review concluded that those requirements are working as intended and that principal-versus-agent and related application judgments stay with the reporting entity's accounting policy (IFRS Foundation, 2024a). This product can supply contract, invoice, usage, and consideration evidence. It cannot decide when revenue is earned.

IFRS 18 assigns presentation categories, required subtotals, and management-defined performance-measure disclosures to the financial statements (IFRS Foundation, 2024b). Statement presentation, taxonomy-versioned reporting, cash-flow classification, and currency translation are accounting concepts. Billing must not encode them.

The Accounting Information Platform therefore owns charts, books, policies, posted journals, close, trial balances, consolidation, and statements. Billing must have an honest export: a journal *proposal*, not a posted book.

## Decision

The Metering Billing Platform exports journal proposals. A separate Accounting Information Platform owns charts, books, policies, posted journals, close, trial balances, consolidation, and statements.

- A proposal is balanced in its transaction currency, carries immutable source identifiers, and uses semantic account roles rather than chart-account identifiers.
- Proposal status cannot be `posted`. Accounting returns a posting receipt (`posted`, `rejected`, `held`, or `reversed`) that Billing stores only as a reconciliation reference.
- IFRS 15 performance-obligation, principal-versus-agent, and revenue-timing judgments belong to versioned accounting policy, not to meters or invoice intent.
- IFRS 18 presentation categories, required subtotals, and management-defined performance measures are Accounting Information Platform concerns. Billing does not encode statement presentation.
- IAS 7 cash-flow classification and IAS 21 transaction, functional, and presentation currencies are applied by Accounting. Billing preserves source currency and amounts and does not infer statutory cash presentation from a payment-provider status.
- Financial-report output remains taxonomy-versioned in Accounting. The 2025 IFRS Accounting Taxonomy remains the claimed reporting taxonomy for 2026 reporting.

No provider webhook can post a general-ledger journal. No accounting rejection can rewrite measured usage or a customer invoice.

## Consequences

- Billing cannot mark a proposal as posted. The product emits journal proposals, not posted books.
- Accounting can reject a proposal without mutating billing facts.
- Source-to-posting reconciliation is explicit and testable.
- Revenue recognition can evolve without changing product metering.
- Operators must not treat an exported proposal, an invoice, or a cash receipt as evidence that statutory revenue has been recognized.
- This repository stays the commercial authority. It does not become the statutory accounting authority.

## References

IFRS Foundation. (2024a). *Post-implementation review of IFRS 15 revenue from contracts with customers*. https://www.ifrs.org/projects/completed-projects/2024/post-implementation-review-of-ifrs-15-revenue-from-contracts-with-customers/

IFRS Foundation. (2024b). *IFRS 18 presentation and disclosure in financial statements*. https://www.ifrs.org/issued-standards/list-of-standards/ifrs-18-presentation-and-disclosure-in-financial-statements/

IFRS Foundation. (2024c). *IFRS 15: Revenue from contracts with customers—Supporting material*. https://www.ifrs.org/supporting-implementation/supporting-materials-by-ifrs-standards/ifrs-15/

IFRS Foundation. (2026). *IFRS Accounting Taxonomy 2025 to remain current for 2026 reporting*. https://www.ifrs.org/news-and-events/news/2026/02/ifrs-accounting-taxonomy-2025-to-remain-current-for-2026/

IFRS Foundation. (n.d.-a). *IAS 7 statement of cash flows*. https://www.ifrs.org/issued-standards/list-of-standards/ias-7-statement-of-cash-flows/

IFRS Foundation. (n.d.-b). *IAS 21 the effects of changes in foreign exchange rates*. https://www.ifrs.org/issued-standards/list-of-standards/ias-21-the-effects-of-changes-in-foreign-exchange-rates/
