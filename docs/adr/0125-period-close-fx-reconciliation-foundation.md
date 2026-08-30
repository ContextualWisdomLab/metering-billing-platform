# ADR 0125: Period-close and FX/reconciliation foundation

## Status

Accepted for the first implementation slice of issue #87.

## Decision

Add immutable, side-effect-free Python contracts for three facts that later
PostgreSQL/provider services can persist atomically:

1. `BillingPeriod` advances only through `open → soft_closed → reconciled →
   invoiced → hard_closed`. Each advance returns a new snapshot and appends an
   actor, authorization reference, reason, and monotonic timestamp. A hard-
   closed snapshot cannot be advanced.
2. `FxRate` records source, rate type, base/quote currencies, exact rate,
   precision, effective time, and recorded time. `FxConversion` copies the
   exact rate and rate identity into the result, then rounds only at the
   explicitly supplied target minor-unit scale with `ROUND_HALF_UP`.
3. `ReconciliationLine` keeps internal expected, provider actual, cash actual,
   provider fee, withholding, and reserve as separate exact amounts. The
   deterministic comparison reports currency, price, fee, or settlement
   exceptions with a next action; provider values never overwrite internal
   expectation.

The contracts are published as Draft 2020-12 schemas and expose no database,
provider, or HTTP side effects. Persistence, maker-checker resolution,
late-event adjustment, FOCUS 1.4 export, and period-wide reconciliation remain
follow-up slices of #87.

The published validators also reconstruct the domain objects after schema
validation, so lifecycle continuity, positive FX rates, exact conversion
arithmetic, reconciliation arithmetic, exception/status consistency, and
contract decimal length limits cannot be bypassed by submitting a raw
dictionary. Reconciliation requires internal, provider, and cash source
currency evidence; a differing source currency must carry a typed
`currency_mismatch` exception.
Every reconciliation exception code is derived from the stored comparison, and
non-negative deduction fields reject signed zero as well as negative values.
Contract versions are positive integers rather than unchecked metadata.

## Consequences

- A database adapter can persist the returned immutable facts without silently
  recomputing a closed-period conversion from a later rate.
- Zero-, two-, three-, and four-decimal target currencies are explicit rather
  than inferred from a display formatter.
- FX multiplication and reconciliation deductions retain exact fixed-point
  precision until the documented target-scale or comparison boundary.
- Reconciliation can distinguish a provider fee mismatch from a settlement
  mismatch while retaining all three source amounts and their currency evidence.
- This ADR does not claim that period close, statutory invoice authority, tax
  calculation, FOCUS export, or GA evidence is complete.
