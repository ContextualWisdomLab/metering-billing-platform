# Agent Development Rules

## Authority

- Preserve the billing-versus-accounting boundary.
- Never use a provider object ID as an internal primary key.
- Never let a webhook directly grant entitlement or post accounting.

## Data

- Use two-or-more-word `snake_case` database identifiers.
- Keep normalized facts relational; raw provider payloads belong in immutable object storage.
- Never store card data, PAT plaintext, prompt text, response text, or provider secrets.
- Use exact decimals for money and billable quantities.

## Development

- Write a failing test before behavior code.
- Require production statement and branch coverage of 100%.
- Document every public API and every accounting or monetary invariant.
- Update architecture, ADRs, and CHANGELOG when authority or behavior changes.
- Keep usage ingestion append-only.  Deduplicate by tenant-scoped source-event key and by source-payload hash plus contract version.
- Rate stored usage through `metering_billing.UsageRatingService`.  Persist append-only rating runs.  Exclude analytics-only and manual-review quality from invoice-intent totals.
- Leave principal, account, and project identifiers usable for invoicing.  Purpose-limit access; do not mask operational billing identifiers.
