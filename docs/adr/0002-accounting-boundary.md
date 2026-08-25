# ADR 0002: Separate Billing from Statutory Accounting

**Status:** Accepted

## Context

Operational billing facts and legal accounting records have different authorities, correction rules, periods, policies, and audit obligations.

## Decision

The Metering Billing Platform exports journal proposals. A separate Accounting Information Platform owns charts, books, policies, posted journals, close, trial balances, consolidation, and statements.

## Consequences

- Billing cannot mark a proposal as posted.
- Accounting can reject a proposal without mutating billing facts.
- Source-to-posting reconciliation is explicit and testable.
- Revenue recognition can evolve without changing product metering.
