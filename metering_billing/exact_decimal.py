"""Exact decimal parsing for billable quantities.

Binary floating-point types are forbidden at the billing boundary.  Quantities
arrive as canonical decimal strings, become :class:`decimal.Decimal` values, and
are compared by exact numeric equality rather than by display text.

See IEEE (2019) and Cowlishaw (2009) in ``docs/doctoring/REFERENCES.md``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from metering_billing.errors import ExactDecimalError, JournalLineAmountScaleError

QUANTITY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
JOURNAL_LINE_AMOUNT_QUANTUM = Decimal("0.000001")
JOURNAL_LINE_AMOUNT_SCALE_ERROR = (
    "journal proposal line amounts cannot exceed six fractional digits"
)


def parse_exact_decimal(quantity_text: str) -> Decimal:
    """Parse a canonical decimal string into an exact non-negative ``Decimal``.

    The function rejects non-strings, scientific notation, signs, NaN, and
    infinity so a producer cannot smuggle an inexact value through ingestion.
    """
    if not isinstance(quantity_text, str):
        raise ExactDecimalError("measured quantity must be a decimal string")
    if QUANTITY_PATTERN.fullmatch(quantity_text) is None:
        raise ExactDecimalError("measured quantity is not a canonical decimal string")
    return Decimal(quantity_text)


def format_exact_decimal(quantity: Decimal) -> str:
    """Render a stored ``Decimal`` as a fixed-point decimal string.

    Scientific notation is never used.  Callers that need to compare stored
    usage should compare ``Decimal`` values, not the rendered strings.
    """
    if not isinstance(quantity, Decimal):
        raise ExactDecimalError("stored quantity must be a Decimal")
    if quantity.is_nan() or quantity.is_infinite() or quantity < 0:
        raise ExactDecimalError("stored quantity must be a finite non-negative decimal")
    return format(quantity, "f")


def require_decimal_quantity(value: Any) -> Decimal:
    """Accept only a ``Decimal`` or a canonical decimal string."""
    if isinstance(value, Decimal):
        return parse_exact_decimal(format_exact_decimal(value))
    return parse_exact_decimal(value)


def journal_line_amount_exceeds_postable_scale(amount: Decimal) -> bool:
    """Return True when *amount* cannot be represented with six fractional digits.

    The stored Exact Decimal is not quantized.  Trailing zeros that do not
    change the value remain postable because AIS ``numeric(38, 6)`` can hold
    them without rounding.  A non-zero digit beyond six places fails closed.
    """
    if amount.is_nan() or amount.is_infinite():
        return True
    return amount != amount.quantize(JOURNAL_LINE_AMOUNT_QUANTUM)


def require_postable_journal_line_amounts(*amounts: Decimal) -> None:
    """Fail closed when any journal-line amount exceeds six fractional digits."""
    for amount in amounts:
        if journal_line_amount_exceeds_postable_scale(amount):
            raise JournalLineAmountScaleError(JOURNAL_LINE_AMOUNT_SCALE_ERROR)
