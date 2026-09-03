# Reconciliation mismatch incident

**Status:** tabletop-ready; live provider, cash, and accounting evidence is required before release.

## Owner

Finance operations owns the mismatch; billing operations supplies internal
usage, invoice, payment, and posting-observation evidence.

## Severity and escalation

Treat an unexplained closed-period difference or missing cash as SEV-1. Treat a
single open exception with no customer impact as SEV-2. Escalate to the AIS or
provider owner as appropriate.

## Customer communication

Hold the affected statement or explain that reconciliation is under review.
Do not present an internal exception as a statutory accounting conclusion.

## Recovery objective

Record the approved period-close RPO/RTO and measured time to isolate and
resolve the exception. Unmeasured targets do not become release evidence.

## Evidence preservation

Capture tenant, billing period, invoice/receipt/proposal IDs, source hashes,
currency, exact amounts, provider reference, and observation timestamps. Keep
all three evidence streams immutable.

## Detection

Compare internal expectation, provider actual, and cash settlement by stable
identity and currency. Track exception count and age rather than silently
rounding or mixing currencies.

## Containment

Prevent close or export of the affected period until the exception is owned.
Do not rewrite an invoice, receipt, provider mapping, or posted journal.

## Diagnosis

Classify timing, identity, amount, currency/FX, duplicate, missing, or
provider-status differences. Verify late events, refunds, disputes, fees, and
unapplied cash independently.

## Recovery

Import the missing authoritative fact or create an explicitly linked exception
and approved adjustment. Re-run the comparison with the same period and
currency boundaries; never hide a difference with a tolerance rule.

## Validation receipt

Record populations, matched/unmatched counts, exact totals by currency,
exception IDs, approvals, command exit statuses, and checksums. Run the
repository validator from the exact release checkout.

## Exit and RCA

Exit when every difference is matched, explicitly excepted, or approved for
adjustment and the period owner signs the receipt. Record the source-system
failure and a prevention test.
