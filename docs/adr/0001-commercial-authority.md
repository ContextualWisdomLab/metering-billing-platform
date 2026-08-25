# ADR 0001: Own Commercial Truth Internally

**Status:** Accepted

## Context

Payment processors, merchant-of-record services, and billing engines expose different customer, meter, invoice, and settlement models.

## Decision

CWL owns canonical usage, meter, price, contract, rating, entitlement, invoice-intent, and reconciliation records. External services are capability adapters and projections.

## Consequences

- Provider migration changes adapters and mappings rather than core identifiers.
- Internal billing can continue when a provider is unavailable.
- CWL must maintain deterministic rating and reconciliation evidence.
