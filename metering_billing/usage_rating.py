"""Deterministic time-windowed rating of already-stored usage.

The service is the buyer-facing read-and-rate path:

1. Resolve the tenant and an effective rate-card version.
2. Load that tenant's stored usage inside a half-open ISO 8601 window.
3. Keep only measurements whose ``meter_quality_rule`` disposition is billable.
4. Multiply exact quantities by exact unit prices into invoice-intent lines.
5. Persist an append-only ``rating_run`` identified by tenant, window, rate card,
   and usage-snapshot hash, or acknowledge an identical replay.

The service does not ingest usage, draft invoices, talk to a payment provider,
or emit a posted accounting journal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    RatingOutcomeCode,
    RatingRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    RateCard,
    StoredRatingLine,
    StoredRatingRun,
    StoredUsageEvent,
    generate_record_id,
)


Clock = Callable[[], datetime]
RATING_CONTRACT_VERSION = 1
DEFAULT_RATE_CARD_CODE = "cwl_standard"


@dataclass(frozen=True)
class RatingLineResult:
    """One invoice-intent line produced from billable usage of a single meter."""

    line_number: int
    billing_account_id: UUID
    billing_account_reference: str
    meter_definition_id: UUID
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the rating-run schema."""
        return {
            "line_number": self.line_number,
            "billing_account_reference": self.billing_account_reference,
            "meter_code": self.meter_code,
            "unit_code": self.unit_code,
            "rated_quantity": format_exact_decimal(self.rated_quantity),
            "unit_price_amount": format_exact_decimal(self.unit_price_amount),
            "line_total_amount": format_exact_decimal(self.line_total_amount),
        }


@dataclass(frozen=True)
class RatingRunResult:
    """Buyer-facing result of rating one tenant window against one rate card."""

    rating_outcome_code: RatingOutcomeCode
    rating_contract_version: int
    rating_run_id: UUID | None
    tenant_reference: str | None
    rate_card_code: str | None
    rate_card_version: int | None
    window_started_at: datetime | None
    window_ended_at: datetime | None
    usage_snapshot_hash: str | None
    currency_code: str | None
    rated_total_amount: Decimal | None
    rejection_reason_code: RatingRejectionReasonCode | None
    rating_lines: tuple[RatingLineResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the rating-run schema."""
        outcome = self.rating_outcome_code
        outcome_text = outcome.value if isinstance(outcome, RatingOutcomeCode) else str(outcome)
        payload: dict[str, object] = {
            "rating_contract_version": self.rating_contract_version,
            "rating_outcome_code": outcome_text,
        }
        if outcome_text == RatingOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else "tenant_not_found"
            )
            return payload
        if outcome_text != RatingOutcomeCode.ACCEPTED and outcome_text != RatingOutcomeCode.DUPLICATE_REPLAY:
            raise ValueError(f"unsupported rating outcome: {outcome_text}")
        payload["rating_run_id"] = str(self.rating_run_id)
        payload["tenant_reference"] = self.tenant_reference
        payload["rate_card_code"] = self.rate_card_code
        payload["rate_card_version"] = self.rate_card_version
        payload["window_started_at"] = _iso_z(self.window_started_at)
        payload["window_ended_at"] = _iso_z(self.window_ended_at)
        payload["usage_snapshot_hash"] = self.usage_snapshot_hash
        payload["currency_code"] = self.currency_code
        payload["rated_total_amount"] = format_exact_decimal(self.rated_total_amount)
        payload["rating_lines"] = [line.as_contract_dict() for line in self.rating_lines]
        return payload


class UsageRatingService:
    """Append-only windowed rater backed by a normalized usage ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
        rate_card_code: str = DEFAULT_RATE_CARD_CODE,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._rate_card_code = rate_card_code

    def rate_usage_window(
        self,
        tenant_reference: str,
        time_window: TimeWindow,
        rate_card_version: int,
        rate_card_code: str | None = None,
    ) -> RatingRunResult:
        """Rate already-stored usage for one tenant inside a half-open window.

        A replay of the same tenant, normalized window, rate-card version, and
        usage snapshot returns the stored ``rating_run_id`` and exact totals.
        """
        resolved_code = self._rate_card_code if rate_card_code is None else rate_card_code
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(RatingRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        window_started_at = time_window.window_started_at.astimezone(UTC)
        window_ended_at = time_window.window_ended_at.astimezone(UTC)
        rate_card, rate_card_error = self.ledger.resolve_rate_card(
            resolved_code, rate_card_version, window_started_at
        )
        if rate_card_error is not None:
            return _rejected(rate_card_error)
        rate_card = require_resolved(rate_card, "rate_card")

        events = self.ledger.list_usage_events_in_window(
            tenant.tenant_account_id, window_started_at, window_ended_at
        )
        usage_snapshot_hash = compute_usage_snapshot_hash(events)
        existing = self.ledger.find_rating_run(
            tenant.tenant_account_id,
            window_started_at,
            window_ended_at,
            rate_card.rate_card_id,
            usage_snapshot_hash,
        )
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                RatingOutcomeCode.DUPLICATE_REPLAY,
            )

        try:
            lines = self._build_rating_lines(rate_card, events)
        except _RatingRejected as error:
            return _rejected(error.reason_code)

        rated_total_amount = sum((line.line_total_amount for line in lines), Decimal("0"))
        rating_run_id = generate_record_id()
        stored_lines = tuple(
            StoredRatingLine(
                rating_line_id=generate_record_id(),
                rating_run_id=rating_run_id,
                tenant_account_id=tenant.tenant_account_id,
                billing_account_id=line.billing_account_id,
                billing_account_reference=line.billing_account_reference,
                meter_definition_id=line.meter_definition_id,
                meter_code=line.meter_code,
                unit_code=line.unit_code,
                rated_quantity=line.rated_quantity,
                unit_price_amount=line.unit_price_amount,
                line_total_amount=line.line_total_amount,
                line_number=line.line_number,
            )
            for line in lines
        )
        stored = self.ledger.insert_rating_run(
            StoredRatingRun(
                rating_run_id=rating_run_id,
                tenant_account_id=tenant.tenant_account_id,
                rate_card_id=rate_card.rate_card_id,
                rate_card_code=rate_card.rate_card_code,
                rate_card_version=rate_card.rate_card_version,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                usage_snapshot_hash=usage_snapshot_hash,
                currency_code=rate_card.currency_code,
                rated_total_amount=rated_total_amount,
                recorded_at=self._clock(),
                rating_lines=stored_lines,
            ),
            stored_lines,
        )
        return _from_stored(stored, tenant.tenant_reference, RatingOutcomeCode.ACCEPTED)

    def _build_rating_lines(
        self,
        rate_card: RateCard,
        events: tuple[StoredUsageEvent, ...],
    ) -> tuple[RatingLineResult, ...]:
        """Aggregate billable measurements into exact invoice-intent lines."""
        aggregates: dict[tuple[UUID, str, UUID, str, str], tuple[Decimal, Decimal]] = {}
        for event in events:
            account_reference = self.ledger.billing_account_reference_for(event.billing_account_id)
            for measurement in event.measurements:
                rule = self.ledger.meter_quality_rules.get(
                    (measurement.meter_definition_id, measurement.quality_code)
                )
                if rule is None:
                    continue
                billable = _is_billable(rule.billing_disposition_code)
                if billable is None:
                    raise _RatingRejected(RatingRejectionReasonCode.BILLING_DISPOSITION_UNKNOWN)
                if not billable:
                    continue
                price = self.ledger.find_rate_card_price(
                    rate_card.rate_card_id, measurement.meter_definition_id
                )
                if price is None:
                    raise _RatingRejected(RatingRejectionReasonCode.METER_PRICE_MISSING)
                key = (
                    event.billing_account_id,
                    account_reference,
                    measurement.meter_definition_id,
                    measurement.meter_code,
                    measurement.unit_code,
                )
                current_quantity, _current_price = aggregates.get(
                    key, (Decimal("0"), price.unit_price_amount)
                )
                aggregates[key] = (current_quantity + measurement.measured_quantity, price.unit_price_amount)

        lines: list[RatingLineResult] = []
        ordered = sorted(aggregates.items(), key=lambda item: (item[0][1], item[0][3]))
        for line_number, (key, (quantity, unit_price_amount)) in enumerate(ordered, start=1):
            billing_account_id, account_reference, meter_definition_id, meter_code, unit_code = key
            lines.append(
                RatingLineResult(
                    line_number=line_number,
                    billing_account_id=billing_account_id,
                    billing_account_reference=account_reference,
                    meter_definition_id=meter_definition_id,
                    meter_code=meter_code,
                    unit_code=unit_code,
                    rated_quantity=quantity,
                    unit_price_amount=unit_price_amount,
                    line_total_amount=quantity * unit_price_amount,
                )
            )
        return tuple(lines)


class _RatingRejected(Exception):
    """Internal control-flow exception for a single failed rating decision."""

    def __init__(self, reason_code: RatingRejectionReasonCode) -> None:
        super().__init__(reason_code.value)
        self.reason_code = reason_code


def compute_usage_snapshot_hash(events: tuple[StoredUsageEvent, ...]) -> str:
    """Return the ``sha256:<hex>`` digest of the tenant window's stored usage."""
    payload = [
        {
            "usage_event_id": str(event.usage_event_id),
            "occurred_at": _iso_z(event.occurred_at),
            "measurements": [
                {
                    "meter_code": measurement.meter_code,
                    "quantity": _canonical_quantity_text(measurement.measured_quantity),
                    "unit_code": measurement.unit_code,
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


def _canonical_quantity_text(quantity: Decimal) -> str:
    """Render a quantity so ``1`` and ``1.0`` produce the same snapshot digest."""
    formatted = format_exact_decimal(quantity)
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def _is_billable(billing_disposition_code: str) -> bool | None:
    """Return whether a quality disposition may enter invoice-intent money."""
    if billing_disposition_code == "billable":
        return True
    if billing_disposition_code == "analytics_only" or billing_disposition_code == "manual_review":
        return False
    return None


def _iso_z(value: datetime) -> str:
    """Render a timezone-aware instant with a ``Z`` UTC suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rejected(reason_code: RatingRejectionReasonCode) -> RatingRunResult:
    """Build a rejected result without writing a rating run."""
    return RatingRunResult(
        rating_outcome_code=RatingOutcomeCode.REJECTED,
        rating_contract_version=RATING_CONTRACT_VERSION,
        rating_run_id=None,
        tenant_reference=None,
        rate_card_code=None,
        rate_card_version=None,
        window_started_at=None,
        window_ended_at=None,
        usage_snapshot_hash=None,
        currency_code=None,
        rated_total_amount=None,
        rejection_reason_code=reason_code,
        rating_lines=(),
    )


def _from_stored(
    stored: StoredRatingRun,
    tenant_reference: str,
    outcome: RatingOutcomeCode,
) -> RatingRunResult:
    """Project a persisted run into the buyer-facing result."""
    return RatingRunResult(
        rating_outcome_code=outcome,
        rating_contract_version=RATING_CONTRACT_VERSION,
        rating_run_id=stored.rating_run_id,
        tenant_reference=tenant_reference,
        rate_card_code=stored.rate_card_code,
        rate_card_version=stored.rate_card_version,
        window_started_at=stored.window_started_at,
        window_ended_at=stored.window_ended_at,
        usage_snapshot_hash=stored.usage_snapshot_hash,
        currency_code=stored.currency_code,
        rated_total_amount=stored.rated_total_amount,
        rejection_reason_code=None,
        rating_lines=tuple(
            RatingLineResult(
                line_number=line.line_number,
                billing_account_id=line.billing_account_id,
                billing_account_reference=line.billing_account_reference,
                meter_definition_id=line.meter_definition_id,
                meter_code=line.meter_code,
                unit_code=line.unit_code,
                rated_quantity=line.rated_quantity,
                unit_price_amount=line.unit_price_amount,
                line_total_amount=line.line_total_amount,
            )
            for line in stored.rating_lines
        ),
    )
