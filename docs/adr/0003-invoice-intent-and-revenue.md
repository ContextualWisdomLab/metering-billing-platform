# ADR 0003: Invoice Intent Is Not Revenue Recognition

**Status:** Accepted

## Context

The product requirements and architecture already treat invoice intent as a commercial fact. Finance operations review invoice intent. The first commercial vertical rates usage into invoice intent, then projects a manual enterprise invoice or a collection-provider invoice. The `invoice_management` bounded context owns invoice intent and explainable lines.

An invoice or cash receipt does not by itself prove that revenue has been earned. Subscription access, prepaid credits, usage obligations, implementation services, and refunds can require different revenue schedules. IFRS 15 is the claimed revenue-from-contracts standard. The 2024 post-implementation review confirmed that the core recognition principles remain suitable and that application judgments—including principal versus agent—stay with accounting policy (IFRS Foundation, 2024a, 2024b).

This platform must therefore keep invoice intent on the billing side of the boundary already decided in [ADR 0002](0002-accounting-boundary.md), and hand Accounting the evidence it needs to apply revenue policy. It must not claim statutory recognition.

## Decision

Invoice intent is a commercial billing fact. Revenue recognition is an accounting fact.

- Billing owns invoice intent, explainable lines, and the usage, contract, and consideration evidence that produced those lines.
- Accounting owns performance-obligation identification, recognition timing, contract-liability versus receivable classification, and principal-versus-agent presentation.
- The handoff is an `accounting_journal_proposal` plus, later, an `accounting_posting_receipt`. The proposal may carry billing, invoice, payment, refund, fee, or settlement evidence. It must not assert that revenue has been posted.
- Changing a collection provider or merchant-of-record role changes mappings and accounting policy. It does not rewrite invoice-intent history or usage history.

This decision uses only the IFRS 15 materials already claimed by the product docs. It does not adopt an additional revenue standard.

## Consequences

- Every invoice line remains explainable down to usage evidence without implying a posted revenue journal.
- Accounting can hold, reject, or reverse a proposal while invoice intent stays unchanged.
- Revenue schedules and contract liabilities can be added in the Accounting Information Platform without changing meters or rating.
- Operators and finance users must read invoice intent as "what we intend to bill," not "what the books have recognized."
- Successor invoice-generation work stays inside commercial rating and invoice management until a proposal is exported.

## References

IFRS Foundation. (2024a). *Post-implementation review of IFRS 15 revenue from contracts with customers*. https://www.ifrs.org/projects/completed-projects/2024/post-implementation-review-of-ifrs-15-revenue-from-contracts-with-customers/

IFRS Foundation. (2024b). *IFRS 15: Revenue from contracts with customers—Supporting material*. https://www.ifrs.org/supporting-implementation/supporting-materials-by-ifrs-standards/ifrs-15/
