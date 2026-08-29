# Incorrect invoice, refund, dispute, settlement, or closed-period adjustment incident

**Status:** tabletop-ready; finance approval and immutable lineage are required before release.

## Owner

Finance operations owns approval; billing operations executes the provider-
neutral commercial correction; AIS owns statutory posting.

## Severity and escalation

Use SEV-1 for a customer or closed-period monetary misstatement. Use SEV-2 for
an open draft or unprocessed correction. Escalate tax or legal-invoice questions
to the designated authority.

## Customer communication

Explain the affected statement, action, and next update. Do not describe a
commercial invoice intent as a statutory invoice or promise provider settlement.

## Recovery objective

Record the approved finance RPO/RTO and measured correction time. A manual
approval without a timed receipt is not release evidence.

## Evidence preservation

Capture original and correction IDs, source hashes, exact amount/currency,
lineage, period, approval, and provider/AIS observation references. Preserve
the original fact unchanged.

## Detection

Compare the customer statement with immutable usage, rating, invoice, payment,
refund, dispute, settlement, and journal-proposal facts.

## Containment

Hold further collection, refund, issue, or close actions for the affected
identity. Never repair by deleting, overwriting, or changing a posted fact.

## Diagnosis

Classify source usage, attribution, rating, tax, provider, cash, timing, or
presentment error. Verify tenant, currency, idempotency, and correction lineage.

## Recovery

Use the supported credit, void, refund, dispute, settlement, or explicitly
authorized adjustment path. Link the replacement to the original and let AIS
own posting and statutory period treatment.

## Validation receipt

Record approval, lineage, exact before/after totals, tenant scope, command exit
statuses, and checksums. Run the repository validator from the exact release
checkout.

## Exit and RCA

Exit when the customer statement and internal lineage agree and all external
owners acknowledge their side. Add a fixture for the original failure and
record whether the period remains open.
