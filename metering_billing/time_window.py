"""Timezone-aware ISO 8601 timestamps and half-open usage windows.

Usage events are point-in-time facts (``occurred_at``).  Ingestion and query
APIs accept an optional closed-open window ``[window_started_at, window_ended_at)``
so a producer can flush an hour or a day without double-counting the boundary
instant.  Naive datetimes are rejected because civil time without an offset is
not an interchange instant (ISO 8601-1:2019).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from metering_billing.errors import TimeWindowError

_SECOND_FRACTION_PATTERN = re.compile(r"T\d{2}:\d{2}:\d{2}\.(\d+)")


def parse_iso8601_datetime(value: Any) -> datetime:
    """Parse a timezone-aware ISO 8601 timestamp.

    The ``Z`` suffix is accepted as UTC.  A missing offset is a hard error.
    """
    if not isinstance(value, str):
        raise TimeWindowError("timestamp must be an ISO 8601 string")
    fraction_match = _SECOND_FRACTION_PATTERN.search(value)
    if fraction_match is not None and len(fraction_match.group(1)) > 6:
        raise TimeWindowError("timestamp cannot contain sub-microsecond precision")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TimeWindowError("timestamp is not a valid ISO 8601 date-time") from error
    if parsed.tzinfo is None:
        raise TimeWindowError("timestamp must include a timezone offset")
    return parsed


@dataclass(frozen=True)
class TimeWindow:
    """Half-open instant range used for batch ingest and usage queries."""

    window_started_at: datetime
    window_ended_at: datetime

    def __post_init__(self) -> None:
        if self.window_started_at.tzinfo is None or self.window_ended_at.tzinfo is None:
            raise TimeWindowError("time window bounds must be timezone-aware")
        if self.window_ended_at <= self.window_started_at:
            raise TimeWindowError("window_ended_at must be after window_started_at")

    def contains(self, occurred_at: datetime) -> bool:
        """Return whether *occurred_at* is inside the half-open window."""
        if occurred_at.tzinfo is None:
            raise TimeWindowError("occurred_at must be timezone-aware")
        return self.window_started_at <= occurred_at < self.window_ended_at

    @classmethod
    def from_iso8601(cls, window_started_at: str, window_ended_at: str) -> TimeWindow:
        """Build a window from ISO 8601 bound strings."""
        return cls(
            window_started_at=parse_iso8601_datetime(window_started_at),
            window_ended_at=parse_iso8601_datetime(window_ended_at),
        )
