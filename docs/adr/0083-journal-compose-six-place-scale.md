# ADR 0083: Journal Compose Fail-Close at Six Fractional Digits

**Status:** Accepted

## Context

AIS Draft #2 (head `fe4c37a`) rejects `debit_amount` and `credit_amount` with more than six fractional digits at its intake boundary. Billing already persists Exact Decimal journal lines on `numeric(38, 12)` and emits canonical decimal strings. Compose could therefore persist a balanced proposal that AIS cannot post.

Quantizing or rounding into `numeric(38, 6)` would change stored commercial money. Counting raw formatted digits or `Decimal.as_tuple().exponent` would also reject trailing zeros that do not change the value (`0.0037050` from `1852.5 * 0.000002`, or `1.0000000`). Those values remain postable because they equal the same amount at six places.

This repository is not the statutory accounting authority. IFRS 15 keeps the export separate from recognition (IFRS Foundation, 2024). IEEE 754 forbids smuggling binary floating-point values into money (IEEE, 2019). PCI DSS keeps card PAN, CVC, and provider secrets off this path (PCI Security Standards Council, 2024).

No VAT register, NTS adapter, AIS posting, `retained_earnings`, 310100, or invented statutory account ID is in scope. Pull stays `GET /v1/journal-proposals`. `proposal_status` stays `validated`. Roles stay semantic.

## Decision

- Fail closed when any journal-line `debit_amount` or `credit_amount` cannot be represented with six fractional digits without changing the stored Exact Decimal: `amount != amount.quantize(Decimal("0.000001"))`.
- Do not round, truncate, or coerce scale. Integers and values with six or fewer significant fractional digits still compose. Trailing zeros that do not change the value still compose.
- Reject at compose (`AccountingExportService` persist helper and `propose_credit_journal`), at credit accept before `insert_credit_adjustment`, and at both in-memory and PostgreSQL `insert_journal_proposal` after debit-XOR-credit and before balance.
- Surface compose rejection as `journal_line_amount_invalid`. Credit accept of an unpostable amount stays `credit_amount_invalid` and writes zero credit and journal rows.
- Keep published JSON Schema digit regex unlimited so emitted trailing zeros remain valid documents. `validate_accounting_journal_proposal` applies the same significant-scale rule after schema and balance checks.
- Do not change `numeric(38, 12)` columns. Do not fold scale into `parse_proposal_amount`. Float money remains `ExactDecimalError`.
- Pin `X-CWL-Tenant-Reference` to the commercial `tenant_account` and fail closed on missing or mismatch. Do not auto-create tenants.

## Consequences

- AIS never pulls an unpostable proposal from this compose path.
- Known morning `0.003705` / `0.0037050` still composes.
- A later AIS statutory mapping or posting slice is unchanged. Billing still emits validated proposals only.
