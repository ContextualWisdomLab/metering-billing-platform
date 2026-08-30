# Accounting Boundary

## Decision

Create a separate `ContextualWisdomLab/accounting-information-platform` rather than extending the Metering Billing Platform into a statutory accounting system.

The split follows the same federated-authority rule used elsewhere in CWL: the system that observes or initiates a transaction publishes evidence, while the system responsible for the legal or managerial fact decides and records that fact.

## Authority stack

| Layer | Authoritative responsibility |
| --- | --- |
| Metering Billing Platform | usage, rating, entitlement, invoice intent, provider payment/refund/dispute state, settlement evidence, and commercial reconciliation |
| Accounting Information Platform | legal entities, accounting books, chart accounts, posting policy, journals, fiscal periods, trial balance, close, and financial statements |
| External authorities and providers | bank transactions, payment-provider records, merchant-of-record tax documents, jurisdictional e-invoices, and regulatory filing receipts |

No provider webhook can post a general-ledger journal. No accounting rejection can rewrite measured usage or a customer invoice. Each authority keeps its own immutable evidence and reconciles across the boundary.

## Why the separation is mandatory

Billing answers:

- Who used which product?
- What quantity was measured?
- Which contract and rate produced the charge?
- What was invoiced, collected, refunded, disputed, and settled?

Accounting answers:

- Which legal entity and accounting book recognize the transaction?
- Which chart accounts and dimensions apply under the approved accounting policy?
- Which fiscal period is open?
- When is revenue recognized rather than merely billed?
- How are transaction, functional, and presentation currencies handled?
- What is the posted journal, trial balance, close status, and financial-statement result?

Combining these questions would couple product pricing to accounting policy, make provider migration dangerous, and allow an operational webhook to affect legal books directly.

## Integration contract

Usage ingestion writes commercial usage facts only.  The rate-card catalog writes commercial `rate_card`, `rate_card_version`, and `rate_card_line` facts only.  Tax assessment writes commercial `tax_rate_schedule`, `tax_rate_version`, and `tax_assessment` facts only and may add a semantic `tax_payable` line to a journal proposal.  Windowed rating writes invoice-intent `rating_run` and `rating_line` facts only.  Invoice draft writes commercial `invoice_draft` facts only.  Accounting export writes an `accounting_journal_proposal` from a persisted draft, payment receipt, credit adjustment, collection write-off, leftover refund, parked leftover, or leftover apply.  Collection writes commercial `collection_case` and `collection_dunning_event` facts only.  Payment intent writes a provider-neutral `payment_intent` only.  Payment settlement writes a commercial `payment_receipt` only and updates collection outstanding.  Credit adjustment writes a commercial `credit_adjustment` and one validated journal proposal only.  A taxed credit may debit semantic `tax_payable`; AIS must map that role.  Invoice-draft presentment is a tenant-scoped read of stored draft, tax, credit, and collection facts.  Tenant API credentials authorize HTTP access and do not post accounting.  Journal-proposal query is a tenant-scoped read of those persisted proposals.  Posting-receipt pull stores an AIS `posting_receipt` as a commercial `posting_receipt_observation` only.  None of those paths mark a journal as posted or capture payment via a named provider.  AIS pulls validated proposals and returns `posting_receipt`; Billing does not flip `proposal_status` after that pull or after storing the observation.  Operators record the credit; AIS pulls the validated three-line unwind.
Late-adjustment presentment is also a tenant-scoped read of stored commercial
evidence; it does not create a journal, mutate a period, or claim statutory
accounting authority.

The billing platform emits `accounting_journal_proposal` with:

- immutable proposal and source IDs;
- legal-entity and intended-book references;
- transaction date, accounting date, transaction currency, and source amounts;
- semantic account roles rather than chart-account IDs;
- balanced debit and credit lines in the proposal currency whose amounts AIS can post without rounding (no more than six significant fractional digits);
- billing, invoice, payment, refund, fee, or settlement evidence;
- an idempotency key, contract version, and payload hash.

The accounting platform returns `accounting_posting_receipt` with:

- posting status (`posted`, `rejected`, `held`, or `reversed`);
- authoritative journal, book, legal-entity, and fiscal-period references;
- accounting-policy and posting-rule versions;
- mapped chart accounts, cost centers, projects, profit centers, and other dimensions;
- transaction, functional, and presentation-currency amounts where applicable;
- posting, rejection, hold, or reversal evidence;
- the exact source proposal ID and payload hash.

The receipt is a reconciliation reference in Billing. It never grants Billing authority to edit a posted journal. Operators pull it after AIS accept; a 404 means the proposal is not yet accepted and must be retried later. Stored `posting_status_code` values remain AIS outcomes and do not change Billing `proposal_status`.

## Accounting data model principles

### Multi-entity and multi-book

A commercial account is not automatically a legal entity, and one legal entity can maintain several books. The accounting system therefore separates:

```text
legal_entity_record
accounting_book
chart_account
accounting_dimension
general_journal
journal_entry_line
fiscal_period
```

Examples of books include primary statutory, tax, management, and consolidation-adjustment books. Posting rules are versioned by entity, book, transaction class, and effective period.

### Dates and immutability

At minimum, accounting records distinguish:

```text
transaction_date
accounting_date
posted_at
reversed_at
recorded_at
```

A posted journal is immutable. Corrections use reversal and replacement journals, preserving the original source and posting lineage. Closed periods reject normal posting and require an explicitly authorized adjustment process.

### Billing is not revenue recognition

An invoice or cash receipt does not by itself prove that revenue has been earned. Subscription access, prepaid credits, usage obligations, implementation services, and refunds can require different revenue schedules. Billing supplies the contract, invoice, usage, and consideration evidence; Accounting applies the approved revenue policy and records receivable, contract liability, revenue, tax, fees, and cash-clearing effects.

### Merchant-of-record and principal-agent evidence

Billing records the provider role, customer-facing seller, provider invoice, payout, tax document, fee, and settlement evidence. Accounting decides the approved gross-versus-net presentation and account mapping. Changing from a merchant of record to a processor therefore changes accounting policy and mappings without changing usage or rating history.

### Bank, tax, and filing adapters

Bank and treasury exchanges belong behind versioned ISO 20022 adapters. Korean electronic tax invoice, VAT, and other jurisdictional functions remain provider or government adapters; jurisdiction-specific rules are not hard-coded into the general ledger core.

## Accounting Information Platform bounded contexts

1. `legal_entity_registry`
2. `accounting_book_registry`
3. `chart_account_registry`
4. `accounting_dimension_registry`
5. `journal_intake_service`
6. `journal_posting_engine`
7. `fiscal_period_control`
8. `currency_accounting`
9. `revenue_accounting`
10. `accounts_receivable_projection`
11. `accounts_payable_management`
12. `cash_reconciliation`
13. `financial_close`
14. `financial_reporting`
15. `accounting_audit_trail`

Management budgeting, payroll calculation, procurement, tax calculation, treasury execution, and regulatory submission may integrate with this platform, but they are not silently absorbed into the general-ledger core.

## First accounting milestone

Do not begin with a full ERP. Build the smallest auditable vertical:

```text
billing journal proposal
-> source and duplicate validation
-> posting-rule resolution
-> legal-entity and book resolution
-> open-period validation
-> balanced multi-line journal
-> immutable posting receipt
-> trial balance
-> reversal
-> source-to-posting reconciliation
-> period close lock
```

The release gate requires that the same proposal cannot create two monetary postings, every posted line belongs to one balanced journal, a closed period cannot accept an ordinary posting, reversal preserves full lineage, and the trial balance reproduces the journal population exactly.

## Delivery order after the first vertical

1. Revenue schedules and contract liabilities.
2. Provider fee, refund, dispute, and settlement accounting.
3. Bank statement import and cash reconciliation.
4. Foreign-currency remeasurement and translation.
5. Accounts payable and controlled expense recognition.
6. Financial close, disclosures, and taxonomy-versioned reporting.
7. Consolidation and intercompany elimination.
8. Management-accounting and profitability projections.
