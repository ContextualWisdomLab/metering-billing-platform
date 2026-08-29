"""Tenant-scoped usage-event presentment projected from stored usage facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``usage_event``.
3. Project quantity, meter, source key, and the next action.
4. Return the event.  Do not ingest, rate, or invent a journal.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import UsageEventPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredUsageEvent


USAGE_EVENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_RATE_WINDOW = "rate_window"


def next_operator_action() -> str:
    """Return rate_window.  Ingest usage, then rate a published card."""
    return OPERATOR_ACTION_RATE_WINDOW


@dataclass(frozen=True)
class UsageEventMeasurementPresentment:
    """One stored measurement projected for operator presentment."""

    meter_code: str
    meter_version: int
    quantity: Decimal
    unit_code: str
    quality_code: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object for one measurement."""
        return {
            "meter_code": self.meter_code,
            "meter_version": self.meter_version,
            "quantity": format_exact_decimal(self.quantity),
            "unit_code": self.unit_code,
            "quality_code": self.quality_code,
        }


@dataclass(frozen=True)
class UsageEventPresentmentResult:
    """Buyer-facing projection of one stored usage event."""

    usage_event_id: UUID
    tenant_reference: str
    source_event_key: str
    event_payload_hash: str
    event_contract_version: int
    producer_contract_version: int
    product_code: str
    occurred_at: datetime
    recorded_at: datetime
    next_operator_action: str
    measurements: tuple[UsageEventMeasurementPresentment, ...]
    operation_code: str | None = None
    cost_center_reference: str | None = None
    project_reference: str | None = None
    repository_reference: str | None = None
    trace_reference: str | None = None
    correlation_reference: str | None = None
    causation_reference: str | None = None
    available_at: datetime | None = None
    dimensions: tuple[tuple[str, str], ...] = ()
    correction_lineage: tuple[tuple[str, str], ...] = ()

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "usage_event_presentment_contract_version": USAGE_EVENT_PRESENTMENT_CONTRACT_VERSION,
            "usage_event_id": str(self.usage_event_id),
            "tenant_reference": self.tenant_reference,
            "source_event_key": self.source_event_key,
            "event_payload_hash": self.event_payload_hash,
            "event_contract_version": self.event_contract_version,
            "producer_contract_version": self.producer_contract_version,
            "product_code": self.product_code,
            "occurred_at": _format_recorded_at(self.occurred_at),
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
            "measurements": [item.as_contract_dict() for item in self.measurements],
        }
        for field_name in (
            "operation_code",
            "cost_center_reference",
            "project_reference",
            "repository_reference",
            "trace_reference",
            "correlation_reference",
            "causation_reference",
        ):
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value
        if self.available_at is not None:
            payload["available_at"] = _format_recorded_at(self.available_at)
        if self.dimensions:
            payload["dimensions"] = dict(self.dimensions)
        if self.correction_lineage:
            payload["correction_lineage"] = dict(self.correction_lineage)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/usage-events``."""
        return {
            "usage_event_id": str(self.usage_event_id),
            "source_event_key": self.source_event_key,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class UsageEventPresentmentPage:
    """One tenant-scoped page of usage-event summaries."""

    usage_events: tuple[UsageEventPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{usage_events, next_cursor}`` with summary items."""
        return {
            "usage_events": [item.as_summary_dict() for item in self.usage_events],
            "next_cursor": self.next_cursor,
        }


class UsageEventPresentmentService:
    """Read-only projector of stored usage events into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_usage_event(
        self, tenant_reference: str, usage_event_id: UUID
    ) -> UsageEventPresentmentResult:
        """Return one same-tenant stored event, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not ingest, rate, or invent a journal.
        """
        tenant = self._require_tenant(tenant_reference)
        event = self.ledger.get_usage_event(usage_event_id)
        if event is None or event.tenant_account_id != tenant.tenant_account_id:
            raise UsageEventPresentmentQueryError("usage_event_not_found")
        return self._project_event(tenant.tenant_reference, event)

    def list_usage_events(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> UsageEventPresentmentPage:
        """Return one tenant page of usage summaries without mutating usage.

        Order is ``recorded_at`` then ``usage_event_id``.  The envelope is
        ``usage_events`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_usage_events(tenant.tenant_account_id),
            key=lambda event: (event.recorded_at, event.usage_event_id),
        )
        matched: list[StoredUsageEvent] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.recorded_at, stored.usage_event_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.recorded_at, last.usage_event_id)
        return UsageEventPresentmentPage(
            usage_events=tuple(
                self._project_event(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise UsageEventPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_event(
        self, tenant_reference: str, event: StoredUsageEvent
    ) -> UsageEventPresentmentResult:
        """Project one stored event using only persisted commercial fields."""
        return UsageEventPresentmentResult(
            usage_event_id=event.usage_event_id,
            tenant_reference=tenant_reference,
            source_event_key=event.source_event_key,
            event_payload_hash=event.event_payload_hash,
            event_contract_version=event.event_contract_version,
            producer_contract_version=event.producer_contract_version,
            product_code=event.product_code,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            next_operator_action=next_operator_action(),
            measurements=tuple(
                UsageEventMeasurementPresentment(
                    meter_code=item.meter_code,
                    meter_version=item.meter_version,
                    quantity=item.measured_quantity,
                    unit_code=item.unit_code,
                    quality_code=item.quality_code,
                )
                for item in event.measurements
            ),
            operation_code=event.operation_code,
            cost_center_reference=event.cost_center_reference,
            project_reference=event.project_reference,
            repository_reference=event.repository_reference,
            trace_reference=event.trace_reference,
            correlation_reference=event.correlation_reference,
            causation_reference=event.causation_reference,
            available_at=event.available_at,
            dimensions=event.dimensions,
            correction_lineage=event.correction_lineage,
        )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Render a usage timestamp as a timezone-aware ISO 8601 instant."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise UsageEventPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise UsageEventPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise UsageEventPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(recorded_at: datetime, usage_event_id: UUID) -> str:
    """Encode the keyset cursor as recorded_at then usage_event_id."""
    return f"{_format_recorded_at(recorded_at)}|{usage_event_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        recorded_text, event_text = cursor.split("|", 1)
        return parse_iso8601_datetime(recorded_text), UUID(event_text)
    except (TypeError, ValueError) as error:
        raise UsageEventPresentmentQueryError("request_invalid") from error
