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
