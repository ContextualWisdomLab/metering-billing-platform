"""Deterministic time-windowed rating of already-stored usage.

The service is the buyer-facing read-and-rate path:

1. Resolve the tenant and a versioned rate card.
2. Select that tenant's stored usage in a half-open ISO 8601 window.
3. Hash the usage snapshot so a replay of the same facts is idempotent.
4. Rate only measurements whose meter quality rule is ``billable``.
5. Persist an append-only ``rating_run`` and ``rating_line`` set.

Invoice-intent totals use exact ``Decimal`` arithmetic.  The service does not
create an invoice draft, talk to a payment provider, or emit a posted journal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable, Never
from uuid import UUID

from metering_billing.errors import BillingDispositionCode, RatingError, RatingOutcomeCode
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredRatingLine,
    StoredRatingRun,
    StoredUsageEvent,
    generate_record_id,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def compute_usage_snapshot_hash(events: tuple[StoredUsageEvent, ...]) -> str:
    """Return the ``sha256:<hex>`` digest of stored usage in rating order."""
    payload = [
        {
            "usage_event_id": str(event.usage_event_id),
            "event_payload_hash": event.event_payload_hash,
            "event_contract_version": event.event_contract_version,
            "occurred_at": _format_instant(event.occurred_at),
            "measurements": [
                {
                    "meter_code": measurement.meter_code,
                    "measured_quantity": format_exact_decimal(measurement.measured_quantity),
                    "quality_code": measurement.quality_code,
                }
                for measurement in event.measurements
            ],
        }
        for event in events
    ]
    canonical_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def enters_invoice_intent(billing_disposition_code: str) -> bool:
    """Return whether a quality disposition may contribute invoice-intent money."""
    try:
        disposition = BillingDispositionCode(billing_disposition_code)
    except ValueError as error:
        raise RatingError(
            f"unknown billing disposition: {billing_disposition_code}"
        ) from error
    match disposition:
        case BillingDispositionCode.BILLABLE:
            return True
        case BillingDispositionCode.ANALYTICS_ONLY | BillingDispositionCode.MANUAL_REVIEW:
            return False
        case _:  # pragma: no cover
            exhausted: Never = disposition
            raise RatingError(f"unhandled billing disposition: {exhausted}")


@dataclass(frozen=True)
class RatingLine:
    """Invoice-intent total for one meter inside a rating run."""

    rating_line_id: UUID
    meter_code: str
    billed_quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal

    def as_contract_dict(self) -> dict[str, str]:
        """Return the closed JSON object published in the rating-run contract."""
        return {
            "meter_code": self.meter_code,
            "billed_quantity": format_exact_decimal(self.billed_quantity),
            "unit_price": format_exact_decimal(self.unit_price),
            "line_amount": format_exact_decimal(self.line_amount),
        }


@dataclass(frozen=True)
class RatingRunResult:
    """Buyer-facing rating result.  Replays reuse ``rating_run_id`` and totals."""

    rating_run_id: UUID
    rating_contract_version: int
    tenant_reference: str
    window_started_at: datetime
    window_ended_at: datetime
    rate_card_code: str
    rate_card_version: int
    usage_snapshot_hash: str
    currency_code: str
    invoice_intent_total: Decimal
    rating_outcome_code: RatingOutcomeCode
    rating_lines: tuple[RatingLine, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the rating-run contract."""
        return {
            "rating_run_id": str(self.rating_run_id),
            "rating_contract_version": self.rating_contract_version,
            "tenant_reference": self.tenant_reference,
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "rate_card_code": self.rate_card_code,
            "rate_card_version": self.rate_card_version,
            "usage_snapshot_hash": self.usage_snapshot_hash,
            "currency_code": self.currency_code,
            "invoice_intent_total": format_exact_decimal(self.invoice_intent_total),
            "rating_outcome_code": self.rating_outcome_code.value,
            "rating_lines": [line.as_contract_dict() for line in self.rating_lines],
        }


class UsageRatingService:
    """Append-only rater backed by already-stored usage and a versioned rate card."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = utc_now if clock is None else clock

    def rate_usage_window(
        self,
        tenant_reference: str,
        time_window: TimeWindow,
        rate_card_code: str,
        rate_card_version: int,
    ) -> RatingRunResult:
        """Rate one tenant's stored usage in ``[start, end)`` against one rate card.

        A second call with the same tenant, window, rate-card version, and usage
        snapshot returns the stored ``rating_run_id`` and the same exact totals.
        """
        try:
            tenant = self.ledger.require_tenant(tenant_reference)
        except KeyError as error:
            raise RatingError(f"tenant is not registered: {tenant_reference}") from error
        rate_card = self.ledger.require_rate_card(rate_card_code, rate_card_version)
        events = self.ledger.list_usage_events_in_window(
            tenant.tenant_account_id,
            time_window.window_started_at,
            time_window.window_ended_at,
        )
        usage_snapshot_hash = compute_usage_snapshot_hash(events)
        existing = self.ledger.find_rating_run(
            tenant.tenant_account_id,
            time_window.window_started_at,
            time_window.window_ended_at,
            rate_card.rate_card_id,
            usage_snapshot_hash,
        )
        if existing is not None:
            return self._result_from_stored(
                existing, tenant_reference, RatingOutcomeCode.DUPLICATE_REPLAY
            )

        lines = self._rate_billable_lines(events, rate_card.rate_card_id)
        invoice_intent_total = sum((line.line_amount for line in lines), Decimal("0"))
        stored_run = StoredRatingRun(
            rating_run_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            window_started_at=time_window.window_started_at,
            window_ended_at=time_window.window_ended_at,
            rate_card_id=rate_card.rate_card_id,
            rate_card_code=rate_card.rate_card_code,
            rate_card_version=rate_card.rate_card_version,
            usage_snapshot_hash=usage_snapshot_hash,
            currency_code=rate_card.currency_code,
            invoice_intent_total=invoice_intent_total,
            recorded_at=self._clock(),
        )
        self.ledger.insert_rating_run(stored_run)
        persisted_lines: list[RatingLine] = []
        for line in lines:
            stored_line = StoredRatingLine(
                rating_line_id=line.rating_line_id,
                tenant_account_id=tenant.tenant_account_id,
                rating_run_id=stored_run.rating_run_id,
                meter_code=line.meter_code,
                billed_quantity=line.billed_quantity,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
            )
            self.ledger.insert_rating_line(stored_line)
            persisted_lines.append(line)
        return RatingRunResult(
            rating_run_id=stored_run.rating_run_id,
            rating_contract_version=1,
            tenant_reference=tenant_reference,
            window_started_at=stored_run.window_started_at,
            window_ended_at=stored_run.window_ended_at,
            rate_card_code=stored_run.rate_card_code,
            rate_card_version=stored_run.rate_card_version,
            usage_snapshot_hash=stored_run.usage_snapshot_hash,
            currency_code=stored_run.currency_code,
            invoice_intent_total=stored_run.invoice_intent_total,
            rating_outcome_code=RatingOutcomeCode.ACCEPTED,
            rating_lines=tuple(persisted_lines),
        )

    def _rate_billable_lines(
        self, events: tuple[StoredUsageEvent, ...], rate_card_id: UUID
    ) -> tuple[RatingLine, ...]:
        """Aggregate billable quantities by meter and apply exact unit prices."""
        billed_quantities: dict[str, Decimal] = {}
        for event in events:
            for measurement in event.measurements:
                rule = self.ledger.meter_quality_rules.get(
                    (measurement.meter_definition_id, measurement.quality_code)
                )
                if rule is None:
                    raise RatingError("meter quality rule is required to rate stored usage")
                if not enters_invoice_intent(rule.billing_disposition_code):
                    continue
                billed_quantities[measurement.meter_code] = (
                    billed_quantities.get(measurement.meter_code, Decimal("0"))
                    + measurement.measured_quantity
                )
        lines: list[RatingLine] = []
        for meter_code in sorted(billed_quantities):
            price = self.ledger.find_rate_card_price(rate_card_id, meter_code)
            if price is None:
                raise RatingError(f"rate card has no unit price for meter: {meter_code}")
            billed_quantity = billed_quantities[meter_code]
            lines.append(
                RatingLine(
                    rating_line_id=generate_record_id(),
                    meter_code=meter_code,
                    billed_quantity=billed_quantity,
                    unit_price=price.unit_price,
                    line_amount=billed_quantity * price.unit_price,
                )
            )
        return tuple(lines)

    def _result_from_stored(
        self,
        stored_run: StoredRatingRun,
        tenant_reference: str,
        outcome: RatingOutcomeCode,
    ) -> RatingRunResult:
        """Rebuild the buyer-facing result from persisted run and line rows."""
        lines = tuple(
            RatingLine(
                rating_line_id=line.rating_line_id,
                meter_code=line.meter_code,
                billed_quantity=line.billed_quantity,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
            )
            for line in self.ledger.list_rating_lines(stored_run.rating_run_id)
        )
        return RatingRunResult(
            rating_run_id=stored_run.rating_run_id,
            rating_contract_version=1,
            tenant_reference=tenant_reference,
            window_started_at=stored_run.window_started_at,
            window_ended_at=stored_run.window_ended_at,
            rate_card_code=stored_run.rate_card_code,
            rate_card_version=stored_run.rate_card_version,
            usage_snapshot_hash=stored_run.usage_snapshot_hash,
            currency_code=stored_run.currency_code,
            invoice_intent_total=stored_run.invoice_intent_total,
            rating_outcome_code=outcome,
            rating_lines=lines,
        )


def _format_instant(value: datetime) -> str:
    """Render a timezone-aware instant with a ``Z`` suffix for UTC."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
