# Standard Traceability

| Standard or source | Engineering decision |
| --- | --- |
| CloudEvents 1.0.2 | Future event envelopes preserve source and event identity for deduplication. |
| Cowlishaw decimal arithmetic 1.70 | Billable quantities are exact decimals, never binary floating-point values. |
| RFC 9110 | At-least-once delivery is made safe by idempotent ingest of the same source key and payload. |
| Helland (2012) | Replays acknowledge the stored fact; a mutated payload is a conflict, not an update. |
| IEEE 754-2019 | Decimal interchange stays in the decimal domain; binary inexact types are rejected. |
| ISO 8601-1:2019 | `occurred_at` and usage windows are timezone-aware instants; naive timestamps fail closed. |
| FOCUS 1.4 | Cost, invoice, billing-period, and commitment data are export projections, not the internal operational schema. |
| IFRS 15 | Billing and revenue recognition remain separate; performance-obligation and principal-versus-agent judgments belong to versioned accounting policy. |
| IFRS 18 | The Accounting Information Platform owns presentation categories, required subtotals, management-defined performance measure disclosures, and the 2027 transition; Billing does not encode statement presentation. |
| IFRS Accounting Taxonomy 2025 | Financial-report output remains taxonomy-versioned, with the 2025 taxonomy retained for 2026 reporting. |
| IAS 7 | Bank and settlement facts are classified and presented by Accounting rather than inferred from a payment-provider status alone. |
| IAS 21 | Transaction, functional, and presentation currencies are separate accounting concepts; Billing preserves source currency and amounts. |
| ISO 20022-1:2026 | Bank and treasury adapters use versioned financial-message mappings rather than proprietary fields in the accounting core. |
| PostgreSQL 18 | UUIDv7 and exact numeric persistence support ordered identifiers and monetary precision. |
