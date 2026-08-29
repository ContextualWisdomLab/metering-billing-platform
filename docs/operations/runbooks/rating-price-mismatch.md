# Rating or price mismatch incident

**Status:** tabletop-ready; live incident evidence is required before release.

## Owner

Billing operations owns the investigation; finance operations approves any
customer-visible correction.

## Severity and escalation

Treat an unexplained monetary difference as SEV-1 when an issued statement is
affected, otherwise SEV-2. Escalate to the pricing-policy owner and preserve a
finance review trail.

## Customer communication

State whether the statement is held, delayed, or corrected. Never disclose
another tenant's data, internal rate-card secrets, or raw provider payloads.

## Recovery objective

Record the approved release profile's RPO/RTO and the measured time to freeze
and restore rating. This document supplies no unmeasured production target.

## Evidence preservation

Capture tenant, billing account, rating-run ID, meter/rate-card versions,
source hashes, exact decimal totals, currency, and half-open window. Keep the
original event and rating facts immutable.

## Detection

Compare the invoice line with the stored rating run, rate-card version, meter
quality policy, and invoice-draft snapshot. Use the same tenant-scoped GET
presentment path used by the operator.

## Containment

Hold issuance or customer correction for the affected window. Do not mutate a
rate card, rating run, invoice draft, or issued snapshot in place.

## Diagnosis

Reproduce from stored hashes and exact-decimal strings. Check late, corrected,
estimated, reconstructed, mixed-currency, and excluded-quality facts before
considering a pricing defect.

## Recovery

Publish a new versioned policy or correction fact, re-rate only the approved
window, and issue a linked credit or void when required. Require finance
approval before changing a customer-facing statement.

## Validation receipt

Record old/new version identifiers, exact totals, hashes, approval, command
exit statuses, and customer scope. Run
`python3 scripts/validate_repository.py .` from the exact release checkout.

## Exit and RCA

Exit after the corrected line traces to immutable usage, attribution, policy,
and evidence. Record whether the defect was source, meter, price, tax, or
presentment logic and add a regression fixture.
