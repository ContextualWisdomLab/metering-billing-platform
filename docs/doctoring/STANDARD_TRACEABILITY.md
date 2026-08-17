# Standard Traceability

Only standards already claimed by the product docs appear here. ADR numbers point at the decision that applies the source. PostgreSQL 18 remains a persistence constraint recorded in the technical requirements and data model; it has no dedicated ADR.

| Standard or source | Engineering decision | ADR |
| --- | --- | --- |
| CloudEvents 1.0.2 | Future event envelopes preserve source and event identity for deduplication. | [0001](../adr/0001-commercial-authority.md) |
| FOCUS 1.4 | Cost, invoice, billing-period, and commitment data are export projections, not the internal operational schema. | [0001](../adr/0001-commercial-authority.md) |
| IFRS 15 | Billing and revenue recognition remain separate; performance-obligation and principal-versus-agent judgments belong to versioned accounting policy. | [0002](../adr/0002-accounting-boundary.md), [0003](../adr/0003-invoice-intent-and-revenue.md) |
| IFRS 18 | The Accounting Information Platform owns presentation categories, required subtotals, management-defined performance measure disclosures, and the 2027 transition; Billing does not encode statement presentation. | [0002](../adr/0002-accounting-boundary.md) |
| IFRS Accounting Taxonomy 2025 | Financial-report output remains taxonomy-versioned, with the 2025 taxonomy retained for 2026 reporting. | [0002](../adr/0002-accounting-boundary.md) |
| IAS 7 | Bank and settlement facts are classified and presented by Accounting rather than inferred from a payment-provider status alone. | [0002](../adr/0002-accounting-boundary.md) |
| IAS 21 | Transaction, functional, and presentation currencies are separate accounting concepts; Billing preserves source currency and amounts. | [0002](../adr/0002-accounting-boundary.md) |
| ISO 20022-1:2026 | Bank and treasury adapters use versioned financial-message mappings rather than proprietary fields in the accounting core. | [0001](../adr/0001-commercial-authority.md) |
| PostgreSQL 18 | UUIDv7 and exact numeric persistence support ordered identifiers and monetary precision. | — |
