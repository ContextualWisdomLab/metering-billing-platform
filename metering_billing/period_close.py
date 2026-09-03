"""Immutable billing-period, FX, and three-way reconciliation contracts.

This module is the first #87 domain slice.  It deliberately has no database or
provider side effects: callers can persist the returned append-only facts in a
transaction boundary without allowing provider amounts to overwrite internal
expectations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from metering_billing.errors import PeriodCloseValidationError

BILLING_PERIOD_CONTRACT_VERSION = 1
FX_RATE_CONTRACT_VERSION = 1
FX_CONVERSION_CONTRACT_VERSION = 1
RECONCILIATION_LINE_CONTRACT_VERSION = 1
RECONCILIATION_RESOLUTION_CONTRACT_VERSION = 1
RECONCILIATION_EVIDENCE_CONTRACT_VERSION = 1
RECONCILIATION_RUN_CONTRACT_VERSION = 1
RECONCILIATION_EXCEPTION_AGING_CONTRACT_VERSION = 1
ROUNDING_MODE = "ROUND_HALF_UP"

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SIGNED_DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_FX_RATE_MAX_LENGTH = 39
_SIGNED_AMOUNT_MAX_LENGTH = 40
_NON_NEGATIVE_AMOUNT_MAX_LENGTH = 39
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class BillingPeriodStatus(StrEnum):
    """Append-only lifecycle states for one tenant billing period."""

    OPEN = "open"
    SOFT_CLOSED = "soft_closed"
    RECONCILED = "reconciled"
    INVOICED = "invoiced"
    HARD_CLOSED = "hard_closed"


class FxRateType(StrEnum):
    """Authority category of a recorded exchange rate."""

    SPOT = "spot"
    ACCOUNTING = "accounting"
    PROVIDER = "provider"


class ReconciliationLineStatus(StrEnum):
    """Deterministic result of comparing one three-way line."""

    MATCHED = "matched"
    EXCEPTION = "exception"


class ReconciliationResolutionStatus(StrEnum):
    """Terminal disposition for one maker-checker exception resolution."""

    RESOLVED = "resolved"
    WAIVED = "waived"


class ReconciliationExceptionCode(StrEnum):
    """Stable typed exception vocabulary for three-way reconciliation."""

    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    TAX_MISMATCH = "tax_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    PAYMENT_MISSING = "payment_missing"
    DUPLICATE_CHARGE = "duplicate_charge"
    REFUND_MISMATCH = "refund_mismatch"
    DISPUTE_MISMATCH = "dispute_mismatch"
    SETTLEMENT_MISMATCH = "settlement_mismatch"
    PROVIDER_FEE_MISMATCH = "provider_fee_mismatch"
    CASH_TIMING_DIFFERENCE = "cash_timing_difference"
    UNMAPPED_PROVIDER_OBJECT = "unmapped_provider_object"


class ReconciliationExceptionAgingBucket(StrEnum):
    """UTC calendar-day bucket for one unresolved reconciliation exception."""

    CURRENT = "current"
    DAYS_1_30 = "days_1_30"
    DAYS_31_60 = "days_31_60"
    DAYS_61_90 = "days_61_90"
    DAYS_90_PLUS = "days_90_plus"


_NEXT_ACTIONS = {
    ReconciliationExceptionCode.QUANTITY_MISMATCH: "Compare provider-accepted quantity with internal usage aggregates.",
    ReconciliationExceptionCode.PRICE_MISMATCH: "Compare rejected usage, rating inputs, and the pinned price version.",
    ReconciliationExceptionCode.TAX_MISMATCH: "Compare tax-inclusive provider facts with the internal tax assessment.",
    ReconciliationExceptionCode.CURRENCY_MISMATCH: "Map the source currencies and record the authoritative FX rate.",
    ReconciliationExceptionCode.PAYMENT_MISSING: "Trace the expected provider payment and its authoritative receipt.",
    ReconciliationExceptionCode.DUPLICATE_CHARGE: "Match duplicate provider charges to the original internal intent.",
    ReconciliationExceptionCode.REFUND_MISMATCH: "Compare the provider refund with the internal refund fact.",
    ReconciliationExceptionCode.DISPUTE_MISMATCH: "Compare provider dispute status and amount with the internal case.",
    ReconciliationExceptionCode.PROVIDER_FEE_MISMATCH: "Request the provider fee or reserve breakdown for this payout.",
    ReconciliationExceptionCode.SETTLEMENT_MISMATCH: "Trace the provider payout to remittance and bank evidence.",
    ReconciliationExceptionCode.CASH_TIMING_DIFFERENCE: "Record the payout timing window and expected settlement date.",
    ReconciliationExceptionCode.UNMAPPED_PROVIDER_OBJECT: "Map the provider object before treating the comparison as complete.",
}

_NEXT_PERIOD_STATUS = {
    BillingPeriodStatus.OPEN: BillingPeriodStatus.SOFT_CLOSED,
    BillingPeriodStatus.SOFT_CLOSED: BillingPeriodStatus.RECONCILED,
    BillingPeriodStatus.RECONCILED: BillingPeriodStatus.INVOICED,
    BillingPeriodStatus.INVOICED: BillingPeriodStatus.HARD_CLOSED,
}


def _reference(value: Any, field_name: str) -> str:
    """Require a non-empty operational reference."""
    if not isinstance(value, str) or not value.strip():
        raise PeriodCloseValidationError(f"{field_name} must be a non-empty string")
    return value


def _tenant_reference(value: Any) -> str:
    """Require the repository's tenant URN boundary."""
    reference = _reference(value, "tenant_reference")
    if not reference.startswith("urn:cwl:"):
        raise PeriodCloseValidationError("tenant_reference must start with urn:cwl:")
    return reference


def _currency(value: Any, field_name: str = "currency_code") -> str:
    """Require an uppercase ISO-4217-shaped currency code."""
    if not isinstance(value, str) or _CURRENCY_PATTERN.fullmatch(value) is None:
        raise PeriodCloseValidationError(f"{field_name} must be an uppercase three-letter code")
    return value


def _aware_datetime(value: Any, field_name: str) -> datetime:
    """Require a timezone-aware datetime so persisted ordering is unambiguous."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PeriodCloseValidationError(f"{field_name} must be timezone-aware")
    return value


def _not_future(value: datetime, field_name: str) -> datetime:
    """Reject audit facts that claim to have been captured in the future."""
    if value > datetime.now(UTC):
        raise PeriodCloseValidationError(f"{field_name} must not be in the future")
    return value


def _contract_version(value: Any, field_name: str) -> int:
    """Require a positive integer contract version at the domain boundary."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PeriodCloseValidationError(f"{field_name} must be a positive integer")
    return value


def _format_datetime(value: datetime) -> str:
    """Render a timezone-aware instant as a stable UTC ISO-8601 string."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _signed_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a finite exact signed decimal without accepting binary floats."""
    if isinstance(value, bool):
        raise PeriodCloseValidationError(f"{field_name} must be an exact decimal")
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, str):
        text = value
    else:
        raise PeriodCloseValidationError(f"{field_name} must be an exact decimal")
    if _SIGNED_DECIMAL_PATTERN.fullmatch(text) is None:
        raise PeriodCloseValidationError(f"{field_name} must be a canonical decimal string")
    return Decimal(text)


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    """Parse an exact decimal that cannot represent a negative cost component."""
    parsed = _signed_decimal(value, field_name)
    if parsed < 0:
        raise PeriodCloseValidationError(f"{field_name} must not be negative")
    return parsed


def _minor_units(value: Any) -> int:
    """Require an explicitly supplied zero- through four-decimal currency scale."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
        raise PeriodCloseValidationError("minor_units must be an integer from 0 through 4")
    return value


def _rate_scale(value: Decimal) -> int:
    """Return the number of fractional digits represented by an exact rate."""
    return max(0, -value.as_tuple().exponent)


def _require_decimal_length(value: Decimal, maximum: int, field_name: str) -> Decimal:
    """Keep exact decimal projections inside their published contract bounds."""
    if len(format(value, "f")) > maximum:
        raise PeriodCloseValidationError(f"{field_name} exceeds the contract length limit")
    return value


def _convert_exact_amount(source: Decimal, rate: Decimal, minor_units: int) -> Decimal:
    """Multiply without context rounding before applying the target currency scale."""
    quantum = Decimal(1).scaleb(-minor_units)
    coefficient_digits = len(source.as_tuple().digits) + len(rate.as_tuple().digits)
    with localcontext() as context:
        context.prec = max(28, coefficient_digits + minor_units + 2)
        return (source * rate).quantize(quantum, rounding=ROUND_HALF_UP)


def _expected_cash_amount(
    provider: Decimal, fee: Decimal, withheld: Decimal, reserve: Decimal
) -> Decimal:
    """Subtract independent deductions without context rounding at the contract limit."""
    with localcontext() as context:
        context.prec = max(
            28,
            len(provider.as_tuple().digits)
            + len(fee.as_tuple().digits)
            + len(withheld.as_tuple().digits)
            + len(reserve.as_tuple().digits)
            + 2,
        )
        return provider - fee - withheld - reserve


@dataclass(frozen=True)
class BillingPeriodTransition:
    """One authorized append-only transition after a period was opened."""

    transition_id: UUID
    from_status: BillingPeriodStatus
    to_status: BillingPeriodStatus
    actor_reference: str
    authorization_reference: str
    reason: str
    transitioned_at: datetime

    def __post_init__(self) -> None:
        """Validate the transition independently before it enters a period history."""
        if not isinstance(self.transition_id, UUID):
            raise PeriodCloseValidationError("transition_id must be a UUID")
        try:
            from_status = BillingPeriodStatus(self.from_status)
            to_status = BillingPeriodStatus(self.to_status)
        except ValueError as error:
            raise PeriodCloseValidationError("period transition status is unsupported") from error
        object.__setattr__(self, "from_status", from_status)
        object.__setattr__(self, "to_status", to_status)
        if from_status == to_status:
            raise PeriodCloseValidationError("period transition must change status")
        if _NEXT_PERIOD_STATUS.get(from_status) != to_status:
            raise PeriodCloseValidationError("period transition must advance one lifecycle state")
        _reference(self.actor_reference, "actor_reference")
        _reference(self.authorization_reference, "authorization_reference")
        _reference(self.reason, "reason")
        _aware_datetime(self.transitioned_at, "transitioned_at")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the stable JSON object for one transition fact."""
        return {
            "transition_id": str(self.transition_id),
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "actor_reference": self.actor_reference,
            "authorization_reference": self.authorization_reference,
            "reason": self.reason,
            "transitioned_at": _format_datetime(self.transitioned_at),
        }


@dataclass(frozen=True)
class BillingPeriod:
    """Immutable period aggregate whose status can only advance by appending history."""

    period_id: UUID
    tenant_reference: str
    period_start: date
    period_end: date
    opened_at: datetime
    opened_by: str
    status: BillingPeriodStatus = BillingPeriodStatus.OPEN
    transitions: tuple[BillingPeriodTransition, ...] = ()
    period_contract_version: int = BILLING_PERIOD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Validate dates, identity, and a contiguous forward-only history."""
        _contract_version(self.period_contract_version, "period_contract_version")
        if not isinstance(self.period_id, UUID):
            raise PeriodCloseValidationError("period_id must be a UUID")
        _tenant_reference(self.tenant_reference)
        if (
            not isinstance(self.period_start, date)
            or isinstance(self.period_start, datetime)
            or not isinstance(self.period_end, date)
            or isinstance(self.period_end, datetime)
        ):
            raise PeriodCloseValidationError("period_start and period_end must be dates")
        if self.period_start >= self.period_end:
            raise PeriodCloseValidationError("period_start must precede period_end")
        opened_at = _aware_datetime(self.opened_at, "opened_at")
        _reference(self.opened_by, "opened_by")
        try:
            status = BillingPeriodStatus(self.status)
        except ValueError as error:
            raise PeriodCloseValidationError("period status is unsupported") from error
        object.__setattr__(self, "status", status)
        if not isinstance(self.transitions, tuple):
            raise PeriodCloseValidationError("transitions must be an immutable tuple")
        current_status = BillingPeriodStatus.OPEN
        current_time = opened_at
        for transition in self.transitions:
            if not isinstance(transition, BillingPeriodTransition):
                raise PeriodCloseValidationError("transitions must contain BillingPeriodTransition values")
            if transition.from_status != current_status:
                raise PeriodCloseValidationError("period transition history is not contiguous")
            if transition.transitioned_at < current_time:
                raise PeriodCloseValidationError("period transition timestamps must be monotonic")
            current_status = transition.to_status
            current_time = transition.transitioned_at
        transition_ids = tuple(transition.transition_id for transition in self.transitions)
        if len(set(transition_ids)) != len(transition_ids):
            raise PeriodCloseValidationError("period transition identifiers must be unique")
        if current_status != status:
            raise PeriodCloseValidationError("period status must equal the last transition")

    def advance(
        self,
        to_status: BillingPeriodStatus,
        *,
        actor_reference: str,
        authorization_reference: str,
        reason: str,
        transitioned_at: datetime,
        transition_id: UUID | None = None,
    ) -> BillingPeriod:
        """Return a new period with one authorized next-state transition appended."""
        try:
            target = BillingPeriodStatus(to_status)
        except ValueError as error:
            raise PeriodCloseValidationError("period status is unsupported") from error
        expected = _NEXT_PERIOD_STATUS.get(self.status)
        if expected is None:
            raise PeriodCloseValidationError("hard_closed periods cannot be mutated")
        if target != expected:
            raise PeriodCloseValidationError(
                f"period status must advance from {self.status.value} to {expected.value}"
            )
        transition = BillingPeriodTransition(
            transition_id=uuid4() if transition_id is None else transition_id,
            from_status=self.status,
            to_status=target,
            actor_reference=actor_reference,
            authorization_reference=authorization_reference,
            reason=reason,
            transitioned_at=transitioned_at,
        )
        return BillingPeriod(
            period_id=self.period_id,
            tenant_reference=self.tenant_reference,
            period_start=self.period_start,
            period_end=self.period_end,
            opened_at=self.opened_at,
            opened_by=self.opened_by,
            status=target,
            transitions=self.transitions + (transition,),
            period_contract_version=self.period_contract_version,
        )

    def as_contract_dict(self) -> dict[str, object]:
        """Return the reproducible current period and its append-only history."""
        return {
            "period_contract_version": self.period_contract_version,
            "period_id": str(self.period_id),
            "tenant_reference": self.tenant_reference,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "opened_at": _format_datetime(self.opened_at),
            "opened_by": self.opened_by,
            "period_status": self.status.value,
            "transitions": [transition.as_contract_dict() for transition in self.transitions],
        }


def create_billing_period(
    tenant_reference: str,
    period_start: date,
    period_end: date,
    *,
    opened_by: str,
    opened_at: datetime,
    period_id: UUID | None = None,
) -> BillingPeriod:
    """Create an open period; all later changes must use :meth:`BillingPeriod.advance`."""
    return BillingPeriod(
        period_id=uuid4() if period_id is None else period_id,
        tenant_reference=tenant_reference,
        period_start=period_start,
        period_end=period_end,
        opened_at=opened_at,
        opened_by=opened_by,
    )


@dataclass(frozen=True)
class FxRate:
    """Versioned exact exchange-rate evidence that later rates cannot rewrite."""

    fx_rate_id: UUID
    rate_source: str
    rate_type: FxRateType
    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_precision: int
    effective_at: datetime
    recorded_at: datetime
    fx_rate_contract_version: int = FX_RATE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Validate exact rate metadata and preserve the supplied versioned value."""
        _contract_version(self.fx_rate_contract_version, "fx_rate_contract_version")
        if not isinstance(self.fx_rate_id, UUID):
            raise PeriodCloseValidationError("fx_rate_id must be a UUID")
        _reference(self.rate_source, "rate_source")
        try:
            rate_type = FxRateType(self.rate_type)
        except ValueError as error:
            raise PeriodCloseValidationError("rate_type is unsupported") from error
        object.__setattr__(self, "rate_type", rate_type)
        _currency(self.base_currency, "base_currency")
        _currency(self.quote_currency, "quote_currency")
        parsed_rate = _non_negative_decimal(self.rate, "rate")
        if parsed_rate <= 0:
            raise PeriodCloseValidationError("rate must be greater than zero")
        object.__setattr__(
            self,
            "rate",
            _require_decimal_length(parsed_rate, _FX_RATE_MAX_LENGTH, "rate"),
        )
        if (
            isinstance(self.rate_precision, bool)
            or not isinstance(self.rate_precision, int)
            or self.rate_precision < _rate_scale(parsed_rate)
        ):
            raise PeriodCloseValidationError("rate_precision must cover the exact rate scale")
        _aware_datetime(self.effective_at, "effective_at")
        _aware_datetime(self.recorded_at, "recorded_at")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the exact rate evidence used by a conversion."""
        return {
            "fx_rate_contract_version": self.fx_rate_contract_version,
            "fx_rate_id": str(self.fx_rate_id),
            "rate_source": self.rate_source,
            "rate_type": self.rate_type.value,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "rate": format(self.rate, "f"),
            "rate_precision": self.rate_precision,
            "effective_at": _format_datetime(self.effective_at),
            "recorded_at": _format_datetime(self.recorded_at),
        }


def create_fx_rate(
    rate_source: str,
    rate_type: FxRateType,
    base_currency: str,
    quote_currency: str,
    rate: Decimal | str,
    rate_precision: int,
    effective_at: datetime,
    recorded_at: datetime,
    *,
    fx_rate_id: UUID | None = None,
) -> FxRate:
    """Record one immutable exact FX rate with explicit source and precision."""
    return FxRate(
        fx_rate_id=uuid4() if fx_rate_id is None else fx_rate_id,
        rate_source=rate_source,
        rate_type=rate_type,
        base_currency=base_currency,
        quote_currency=quote_currency,
        rate=rate if isinstance(rate, Decimal) else _signed_decimal(rate, "rate"),
        rate_precision=rate_precision,
        effective_at=effective_at,
        recorded_at=recorded_at,
    )


@dataclass(frozen=True)
class FxConversion:
    """Frozen FX result carrying the rate value and identity used to compute it."""

    fx_conversion_id: UUID
    fx_rate_id: UUID
    source_amount: Decimal
    source_currency: str
    quote_amount: Decimal
    quote_currency: str
    quote_minor_units: int
    rate: Decimal
    rate_precision: int
    converted_at: datetime
    fx_conversion_contract_version: int = FX_CONVERSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Validate a conversion result without recalculating or mutating it."""
        _contract_version(
            self.fx_conversion_contract_version, "fx_conversion_contract_version"
        )
        if not isinstance(self.fx_conversion_id, UUID) or not isinstance(self.fx_rate_id, UUID):
            raise PeriodCloseValidationError("FX conversion identifiers must be UUIDs")
        object.__setattr__(
            self,
            "source_amount",
            _require_decimal_length(
                _signed_decimal(self.source_amount, "source_amount"),
                _SIGNED_AMOUNT_MAX_LENGTH,
                "source_amount",
            ),
        )
        object.__setattr__(
            self,
            "quote_amount",
            _require_decimal_length(
                _signed_decimal(self.quote_amount, "quote_amount"),
                _SIGNED_AMOUNT_MAX_LENGTH,
                "quote_amount",
            ),
        )
        _currency(self.source_currency, "source_currency")
        _currency(self.quote_currency, "quote_currency")
        minor_units = _minor_units(self.quote_minor_units)
        object.__setattr__(
            self,
            "rate",
            _require_decimal_length(
                _non_negative_decimal(self.rate, "rate"),
                _FX_RATE_MAX_LENGTH,
                "rate",
            ),
        )
        if self.rate <= 0:
            raise PeriodCloseValidationError("rate must be greater than zero")
        if (
            isinstance(self.rate_precision, bool)
            or not isinstance(self.rate_precision, int)
            or self.rate_precision < _rate_scale(self.rate)
        ):
            raise PeriodCloseValidationError("rate_precision must cover the exact rate scale")
        if _rate_scale(self.quote_amount) > minor_units:
            raise PeriodCloseValidationError("quote_amount exceeds quote_minor_units scale")
        if self.quote_amount != _convert_exact_amount(self.source_amount, self.rate, minor_units):
            raise PeriodCloseValidationError("quote_amount must equal the rounded source amount at the pinned rate")
        _aware_datetime(self.converted_at, "converted_at")

    def as_contract_dict(self) -> dict[str, object]:
        """Return a replayable conversion result with no implicit live-rate lookup."""
        return {
            "fx_conversion_contract_version": self.fx_conversion_contract_version,
            "fx_conversion_id": str(self.fx_conversion_id),
            "fx_rate_id": str(self.fx_rate_id),
            "source_amount": format(self.source_amount, "f"),
            "source_currency": self.source_currency,
            "quote_amount": format(self.quote_amount, "f"),
            "quote_currency": self.quote_currency,
            "quote_minor_units": self.quote_minor_units,
            "rate": format(self.rate, "f"),
            "rate_precision": self.rate_precision,
            "rounding_mode": ROUNDING_MODE,
            "converted_at": _format_datetime(self.converted_at),
        }


def convert_currency_amount(
    source_amount: Decimal | str,
    source_currency: str,
    target_minor_units: int,
    fx_rate: FxRate,
    *,
    fx_conversion_id: UUID | None = None,
    converted_at: datetime | None = None,
) -> FxConversion:
    """Convert one exact amount using a pinned rate and explicit target scale."""
    if not isinstance(fx_rate, FxRate):
        raise PeriodCloseValidationError("fx_rate must be an FxRate")
    source_code = _currency(source_currency, "source_currency")
    if source_code != fx_rate.base_currency:
        raise PeriodCloseValidationError("source_currency must match FX rate base_currency")
    source = _signed_decimal(source_amount, "source_amount")
    minor_units = _minor_units(target_minor_units)
    quote = _convert_exact_amount(source, fx_rate.rate, minor_units)
    return FxConversion(
        fx_conversion_id=uuid4() if fx_conversion_id is None else fx_conversion_id,
        fx_rate_id=fx_rate.fx_rate_id,
        source_amount=source,
        source_currency=source_code,
        quote_amount=quote,
        quote_currency=fx_rate.quote_currency,
        quote_minor_units=minor_units,
        rate=fx_rate.rate,
        rate_precision=fx_rate.rate_precision,
        converted_at=fx_rate.recorded_at if converted_at is None else converted_at,
    )


@dataclass(frozen=True)
class ReconciliationException:
    """One typed reconciliation exception with an actionable next step."""

    exception_code: ReconciliationExceptionCode
    next_action: str

    def __post_init__(self) -> None:
        """Validate the stable exception code and operator guidance."""
        try:
            code = ReconciliationExceptionCode(self.exception_code)
        except ValueError as error:
            raise PeriodCloseValidationError("reconciliation exception code is unsupported") from error
        object.__setattr__(self, "exception_code", code)
        if self.next_action != _NEXT_ACTIONS[code]:
            raise PeriodCloseValidationError("next_action must match the reconciliation exception code")
        _reference(self.next_action, "next_action")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the exception code and next action for an operator queue."""
        return {
            "exception_code": self.exception_code.value,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class ReconciliationExceptionAging:
    """Immutable age projection derived from a line's assessment timestamp."""

    reconciliation_line_id: UUID
    period_id: UUID
    exception_code: ReconciliationExceptionCode
    next_action: str
    assessed_at: datetime
    as_of: datetime
    age_days: int
    aging_bucket: ReconciliationExceptionAgingBucket
    reconciliation_exception_aging_contract_version: int = (
        RECONCILIATION_EXCEPTION_AGING_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        """Require a non-negative, UTC-calendar age consistent with the source line."""
        _contract_version(
            self.reconciliation_exception_aging_contract_version,
            "reconciliation_exception_aging_contract_version",
        )
        if not isinstance(self.reconciliation_line_id, UUID) or not isinstance(
            self.period_id, UUID
        ):
            raise PeriodCloseValidationError("aging identifiers must be UUIDs")
        try:
            exception_code = ReconciliationExceptionCode(self.exception_code)
            aging_bucket = ReconciliationExceptionAgingBucket(self.aging_bucket)
        except ValueError as error:
            raise PeriodCloseValidationError(
                "reconciliation exception aging code or bucket is unsupported"
            ) from error
        object.__setattr__(self, "exception_code", exception_code)
        object.__setattr__(self, "aging_bucket", aging_bucket)
        _reference(self.next_action, "next_action")
        assessed_at = _aware_datetime(self.assessed_at, "assessed_at")
        as_of = _aware_datetime(self.as_of, "as_of")
        if as_of < assessed_at:
            raise PeriodCloseValidationError("as_of must not precede assessed_at")
        if (
            isinstance(self.age_days, bool)
            or not isinstance(self.age_days, int)
            or self.age_days < 0
        ):
            raise PeriodCloseValidationError("age_days must be a non-negative integer")
        expected_age_days = _calendar_age_days(assessed_at, as_of)
        if self.age_days != expected_age_days:
            raise PeriodCloseValidationError("age_days must match assessed_at and as_of")
        if self.aging_bucket != _exception_aging_bucket(self.age_days):
            raise PeriodCloseValidationError("aging_bucket must match age_days")
        ReconciliationException(exception_code, self.next_action)

    def as_contract_dict(self) -> dict[str, object]:
        """Return the deterministic exception-aging projection contract."""
        return {
            "reconciliation_exception_aging_contract_version": (
                self.reconciliation_exception_aging_contract_version
            ),
            "reconciliation_line_id": str(self.reconciliation_line_id),
            "period_id": str(self.period_id),
            "exception_code": self.exception_code.value,
            "next_action": self.next_action,
            "assessed_at": _format_datetime(self.assessed_at),
            "as_of": _format_datetime(self.as_of),
            "age_days": self.age_days,
            "aging_bucket": self.aging_bucket.value,
        }


def age_reconciliation_exception(
    line: ReconciliationLine,
    exception_code: ReconciliationExceptionCode | str,
    as_of: datetime,
) -> ReconciliationExceptionAging:
    """Project one persisted line exception into a deterministic age bucket."""
    if not isinstance(line, ReconciliationLine):
        raise PeriodCloseValidationError("line must be a ReconciliationLine")
    try:
        code = ReconciliationExceptionCode(exception_code)
    except ValueError as error:
        raise PeriodCloseValidationError("reconciliation exception code is unsupported") from error
    exception = next(
        (item for item in line.exceptions if item.exception_code == code),
        None,
    )
    if exception is None:
        raise PeriodCloseValidationError("exception code is not present on the reconciliation line")
    assessed_at = _aware_datetime(line.assessed_at, "assessed_at")
    observed_as_of = _aware_datetime(as_of, "as_of")
    if observed_as_of < assessed_at:
        raise PeriodCloseValidationError("as_of must not precede assessed_at")
    age_days = _calendar_age_days(assessed_at, observed_as_of)
    return ReconciliationExceptionAging(
        reconciliation_line_id=line.reconciliation_line_id,
        period_id=line.period_id,
        exception_code=exception.exception_code,
        next_action=exception.next_action,
        assessed_at=assessed_at,
        as_of=observed_as_of,
        age_days=age_days,
        aging_bucket=_exception_aging_bucket(age_days),
    )


def _calendar_age_days(assessed_at: datetime, as_of: datetime) -> int:
    """Return elapsed UTC calendar days, matching finance aging conventions."""
    return (as_of.astimezone(UTC).date() - assessed_at.astimezone(UTC).date()).days


def _exception_aging_bucket(age_days: int) -> ReconciliationExceptionAgingBucket:
    """Map a non-negative age to the published exception-aging bucket."""
    if age_days <= 0:
        return ReconciliationExceptionAgingBucket.CURRENT
    if age_days <= 30:
        return ReconciliationExceptionAgingBucket.DAYS_1_30
    if age_days <= 60:
        return ReconciliationExceptionAgingBucket.DAYS_31_60
    if age_days <= 90:
        return ReconciliationExceptionAgingBucket.DAYS_61_90
    return ReconciliationExceptionAgingBucket.DAYS_90_PLUS


@dataclass(frozen=True)
class ReconciliationEvidence:
    """Immutable hash-backed source evidence for one reconciliation exception."""

    evidence_id: UUID
    reconciliation_line_id: UUID
    exception_code: ReconciliationExceptionCode
    evidence_kind: str
    evidence_reference: str
    evidence_sha256: str
    captured_by: str
    captured_at: datetime
    reconciliation_evidence_contract_version: int = (
        RECONCILIATION_EVIDENCE_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        """Require a source reference and a verifiable content hash."""
        _contract_version(
            self.reconciliation_evidence_contract_version,
            "reconciliation_evidence_contract_version",
        )
        if not isinstance(self.evidence_id, UUID) or not isinstance(
            self.reconciliation_line_id, UUID
        ):
            raise PeriodCloseValidationError("evidence identifiers must be UUIDs")
        try:
            exception_code = ReconciliationExceptionCode(self.exception_code)
        except ValueError as error:
            raise PeriodCloseValidationError("reconciliation evidence code is unsupported") from error
        object.__setattr__(self, "exception_code", exception_code)
        _reference(self.evidence_kind, "evidence_kind")
        _reference(self.evidence_reference, "evidence_reference")
        if not isinstance(self.evidence_sha256, str) or _SHA256_PATTERN.fullmatch(
            self.evidence_sha256
        ) is None:
            raise PeriodCloseValidationError("evidence_sha256 must be a sha256 digest")
        _reference(self.captured_by, "captured_by")
        _not_future(_aware_datetime(self.captured_at, "captured_at"), "captured_at")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the immutable source-evidence contract."""
        return {
            "reconciliation_evidence_contract_version": self.reconciliation_evidence_contract_version,
            "evidence_id": str(self.evidence_id),
            "reconciliation_line_id": str(self.reconciliation_line_id),
            "exception_code": self.exception_code.value,
            "evidence_kind": self.evidence_kind,
            "evidence_reference": self.evidence_reference,
            "evidence_sha256": self.evidence_sha256,
            "captured_by": self.captured_by,
            "captured_at": _format_datetime(self.captured_at),
        }


@dataclass(frozen=True)
class ReconciliationRun:
    """Immutable completed run containing an ordered set of reconciliation lines."""

    run_id: UUID
    period_id: UUID
    started_at: datetime
    completed_at: datetime
    reconciliation_line_ids: tuple[UUID, ...]
    blocking_exception_count: int
    reconciliation_run_contract_version: int = RECONCILIATION_RUN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Require ordered unique lines and a non-negative exception summary."""
        _contract_version(
            self.reconciliation_run_contract_version,
            "reconciliation_run_contract_version",
        )
        if not isinstance(self.run_id, UUID) or not isinstance(self.period_id, UUID):
            raise PeriodCloseValidationError("run identifiers must be UUIDs")
        _aware_datetime(self.started_at, "started_at")
        _aware_datetime(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise PeriodCloseValidationError("completed_at must not precede started_at")
        if not isinstance(self.reconciliation_line_ids, tuple) or any(
            not isinstance(line_id, UUID) for line_id in self.reconciliation_line_ids
        ):
            raise PeriodCloseValidationError("reconciliation_line_ids must contain UUIDs")
        if len(set(self.reconciliation_line_ids)) != len(self.reconciliation_line_ids):
            raise PeriodCloseValidationError("reconciliation line identifiers must be unique")
        if (
            isinstance(self.blocking_exception_count, bool)
            or not isinstance(self.blocking_exception_count, int)
            or self.blocking_exception_count < 0
        ):
            raise PeriodCloseValidationError("blocking_exception_count must be non-negative")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the completed run and its ordered line membership."""
        return {
            "reconciliation_run_contract_version": self.reconciliation_run_contract_version,
            "run_id": str(self.run_id),
            "period_id": str(self.period_id),
            "started_at": _format_datetime(self.started_at),
            "completed_at": _format_datetime(self.completed_at),
            "reconciliation_line_ids": [str(line_id) for line_id in self.reconciliation_line_ids],
            "blocking_exception_count": self.blocking_exception_count,
        }


@dataclass(frozen=True)
class ReconciliationResolution:
    """Immutable maker-checker disposition for one reconciliation exception."""

    resolution_id: UUID
    reconciliation_line_id: UUID
    exception_code: ReconciliationExceptionCode
    resolution_status: ReconciliationResolutionStatus
    owner_reference: str
    resolution_reason: str
    evidence_reference: str
    maker_reference: str
    checker_reference: str
    resolved_at: datetime
    reconciliation_resolution_contract_version: int = (
        RECONCILIATION_RESOLUTION_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        """Require an explicit exception disposition and distinct approvers."""
        _contract_version(
            self.reconciliation_resolution_contract_version,
            "reconciliation_resolution_contract_version",
        )
        if not isinstance(self.resolution_id, UUID) or not isinstance(
            self.reconciliation_line_id, UUID
        ):
            raise PeriodCloseValidationError("resolution identifiers must be UUIDs")
        try:
            exception_code = ReconciliationExceptionCode(self.exception_code)
            resolution_status = ReconciliationResolutionStatus(self.resolution_status)
        except ValueError as error:
            raise PeriodCloseValidationError(
                "reconciliation resolution code or status is unsupported"
            ) from error
        object.__setattr__(self, "exception_code", exception_code)
        object.__setattr__(self, "resolution_status", resolution_status)
        for field_name in (
            "owner_reference",
            "resolution_reason",
            "evidence_reference",
            "maker_reference",
            "checker_reference",
        ):
            _reference(getattr(self, field_name), field_name)
        if self.maker_reference == self.checker_reference:
            raise PeriodCloseValidationError("maker and checker references must differ")
        _not_future(_aware_datetime(self.resolved_at, "resolved_at"), "resolved_at")

    def as_contract_dict(self) -> dict[str, object]:
        """Return the complete maker-checker disposition evidence."""
        return {
            "reconciliation_resolution_contract_version": self.reconciliation_resolution_contract_version,
            "resolution_id": str(self.resolution_id),
            "reconciliation_line_id": str(self.reconciliation_line_id),
            "exception_code": self.exception_code.value,
            "resolution_status": self.resolution_status.value,
            "owner_reference": self.owner_reference,
            "resolution_reason": self.resolution_reason,
            "evidence_reference": self.evidence_reference,
            "maker_reference": self.maker_reference,
            "checker_reference": self.checker_reference,
            "resolved_at": _format_datetime(self.resolved_at),
        }


@dataclass(frozen=True)
class ReconciliationLine:
    """One provider/account/period/currency line with immutable comparison inputs."""

    reconciliation_line_id: UUID
    period_id: UUID
    provider_account_reference: str
    currency_code: str
    internal_expected_amount: Decimal
    provider_actual_amount: Decimal
    cash_actual_amount: Decimal
    provider_fee_amount: Decimal
    withheld_tax_amount: Decimal
    reserve_amount: Decimal
    expected_cash_amount: Decimal
    status: ReconciliationLineStatus
    exceptions: tuple[ReconciliationException, ...]
    assessed_at: datetime
    internal_currency_code: str
    provider_currency_code: str
    cash_currency_code: str
    reconciliation_line_contract_version: int = RECONCILIATION_LINE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        """Validate exact amounts and the deterministic status/exception invariant."""
        _contract_version(
            self.reconciliation_line_contract_version,
            "reconciliation_line_contract_version",
        )
        if not isinstance(self.reconciliation_line_id, UUID) or not isinstance(self.period_id, UUID):
            raise PeriodCloseValidationError("reconciliation identifiers must be UUIDs")
        _reference(self.provider_account_reference, "provider_account_reference")
        _currency(self.currency_code)
        for field_name in (
            "internal_expected_amount",
            "provider_actual_amount",
            "cash_actual_amount",
            "expected_cash_amount",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_decimal_length(
                    _signed_decimal(getattr(self, field_name), field_name),
                    _SIGNED_AMOUNT_MAX_LENGTH,
                    field_name,
                ),
            )
        for field_name in ("provider_fee_amount", "withheld_tax_amount", "reserve_amount"):
            object.__setattr__(
                self,
                field_name,
                _require_decimal_length(
                    _non_negative_decimal(getattr(self, field_name), field_name),
                    _NON_NEGATIVE_AMOUNT_MAX_LENGTH,
                    field_name,
                ),
            )
        expected_cash = _expected_cash_amount(
            self.provider_actual_amount,
            self.provider_fee_amount,
            self.withheld_tax_amount,
            self.reserve_amount,
        )
        if self.expected_cash_amount != expected_cash:
            raise PeriodCloseValidationError("expected_cash_amount must equal provider actual less deductions")
        try:
            status = ReconciliationLineStatus(self.status)
        except ValueError as error:
            raise PeriodCloseValidationError("reconciliation line status is unsupported") from error
        object.__setattr__(self, "status", status)
        if not isinstance(self.exceptions, tuple) or any(
            not isinstance(exception, ReconciliationException) for exception in self.exceptions
        ):
            raise PeriodCloseValidationError("exceptions must contain immutable reconciliation exceptions")
        if (status == ReconciliationLineStatus.MATCHED) != (not self.exceptions):
            raise PeriodCloseValidationError("reconciliation status must match exception presence")
        exception_codes = tuple(exception.exception_code for exception in self.exceptions)
        if len(set(exception_codes)) != len(exception_codes):
            raise PeriodCloseValidationError("reconciliation exception codes must be unique")
        _aware_datetime(self.assessed_at, "assessed_at")
        currency_mismatch = False
        for field_name, value in (
            ("internal_currency_code", self.internal_currency_code),
            ("provider_currency_code", self.provider_currency_code),
            ("cash_currency_code", self.cash_currency_code),
        ):
            currency_mismatch = currency_mismatch or _currency(value, field_name) != self.currency_code
        has_currency_exception = any(
            exception.exception_code == ReconciliationExceptionCode.CURRENCY_MISMATCH
            for exception in self.exceptions
        )
        if currency_mismatch != has_currency_exception:
            raise PeriodCloseValidationError(
                "currency mismatch evidence must match currency_mismatch exception"
            )

    def as_contract_dict(self) -> dict[str, object]:
        """Return exact comparison values and typed exception evidence."""
        payload: dict[str, object] = {
            "reconciliation_line_contract_version": self.reconciliation_line_contract_version,
            "reconciliation_line_id": str(self.reconciliation_line_id),
            "period_id": str(self.period_id),
            "provider_account_reference": self.provider_account_reference,
            "currency_code": self.currency_code,
            "internal_expected_amount": format(self.internal_expected_amount, "f"),
            "provider_actual_amount": format(self.provider_actual_amount, "f"),
            "cash_actual_amount": format(self.cash_actual_amount, "f"),
            "provider_fee_amount": format(self.provider_fee_amount, "f"),
            "withheld_tax_amount": format(self.withheld_tax_amount, "f"),
            "reserve_amount": format(self.reserve_amount, "f"),
            "expected_cash_amount": format(self.expected_cash_amount, "f"),
            "reconciliation_line_status": self.status.value,
            "exceptions": [exception.as_contract_dict() for exception in self.exceptions],
            "assessed_at": _format_datetime(self.assessed_at),
        }
        for field_name in ("internal_currency_code", "provider_currency_code", "cash_currency_code"):
            payload[field_name] = getattr(self, field_name)
        return payload


def assess_reconciliation_line(
    period_id: UUID,
    provider_account_reference: str,
    currency_code: str,
    internal_expected_amount: Decimal | str,
    provider_actual_amount: Decimal | str,
    cash_actual_amount: Decimal | str,
    *,
    provider_fee_amount: Decimal | str = Decimal("0"),
    withheld_tax_amount: Decimal | str = Decimal("0"),
    reserve_amount: Decimal | str = Decimal("0"),
    assessed_at: datetime,
    reconciliation_line_id: UUID | None = None,
    internal_currency_code: str,
    provider_currency_code: str,
    cash_currency_code: str,
) -> ReconciliationLine:
    """Assess one line without netting provider fees into internal expected revenue."""
    if not isinstance(period_id, UUID):
        raise PeriodCloseValidationError("period_id must be a UUID")
    internal = _signed_decimal(internal_expected_amount, "internal_expected_amount")
    provider = _signed_decimal(provider_actual_amount, "provider_actual_amount")
    cash = _signed_decimal(cash_actual_amount, "cash_actual_amount")
    fee = _non_negative_decimal(provider_fee_amount, "provider_fee_amount")
    withheld = _non_negative_decimal(withheld_tax_amount, "withheld_tax_amount")
    reserve = _non_negative_decimal(reserve_amount, "reserve_amount")
    expected_cash = _expected_cash_amount(provider, fee, withheld, reserve)
    exception_codes: list[ReconciliationExceptionCode] = []
    compared_currencies = (
        ("internal_currency_code", internal_currency_code),
        ("provider_currency_code", provider_currency_code),
        ("cash_currency_code", cash_currency_code),
    )
    for field_name, source_currency in compared_currencies:
        if _currency(source_currency, field_name) != currency_code:
            exception_codes.append(ReconciliationExceptionCode.CURRENCY_MISMATCH)
            break
    if internal != provider:
        exception_codes.append(ReconciliationExceptionCode.PRICE_MISMATCH)
    if cash != expected_cash:
        exception_codes.append(
            ReconciliationExceptionCode.PROVIDER_FEE_MISMATCH
            if fee > 0 and cash == _expected_cash_amount(provider, Decimal("0"), withheld, reserve)
            else ReconciliationExceptionCode.SETTLEMENT_MISMATCH
        )
    exceptions = tuple(
        ReconciliationException(code, _NEXT_ACTIONS[code]) for code in dict.fromkeys(exception_codes)
    )
    return ReconciliationLine(
        reconciliation_line_id=uuid4() if reconciliation_line_id is None else reconciliation_line_id,
        period_id=period_id,
        provider_account_reference=provider_account_reference,
        currency_code=currency_code,
        internal_expected_amount=internal,
        provider_actual_amount=provider,
        cash_actual_amount=cash,
        provider_fee_amount=fee,
        withheld_tax_amount=withheld,
        reserve_amount=reserve,
        expected_cash_amount=expected_cash,
        status=ReconciliationLineStatus.MATCHED
        if not exceptions
        else ReconciliationLineStatus.EXCEPTION,
        exceptions=exceptions,
        assessed_at=assessed_at,
        internal_currency_code=internal_currency_code,
        provider_currency_code=provider_currency_code,
        cash_currency_code=cash_currency_code,
    )
