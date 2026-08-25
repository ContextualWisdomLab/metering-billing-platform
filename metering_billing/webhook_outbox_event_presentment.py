"""Tenant-scoped webhook-outbox-event presentment from stored commercial rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``webhook_outbox_event``.
3. Project identity, event type, source, payload hash, timestamps, and action.
4. Return metadata.  Do not publish, send, retry, or mark delivered.

This is the Billing commercial webhook outbox, not the AIS posting-receipt
outbox.  RFC 9110 treats GET as a safe, idempotent read (Fielding et al.,
2022).  List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import WebhookOutboxEventPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredWebhookOutboxEvent


WEBHOOK_OUTBOX_EVENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_RUN_DELIVERIES = "run_deliveries"


def next_operator_action(*, delivery_status: str) -> str:
    """Return run_deliveries while pending, otherwise wait.

    The next action stays on the #24 deliver run.  It does not invent a
    send, retry, or mark-delivered command.
    """
    if delivery_status == "pending":
        return OPERATOR_ACTION_RUN_DELIVERIES
    if delivery_status == "delivered":
        return OPERATOR_ACTION_WAIT
    raise WebhookOutboxEventPresentmentQueryError("request_invalid")


@dataclass(frozen=True)
class WebhookOutboxEventPresentmentResult:
    """Buyer-facing projection of one stored commercial webhook outbox event."""

    outbox_event_id: UUID
    tenant_reference: str
    event_type_code: str
    source_id: UUID
    payload_hash: str
    occurred_at: datetime
    enqueued_at: datetime
    delivery_status: str
    attempted_delivery_count: int
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "webhook_outbox_event_presentment_contract_version": (
                WEBHOOK_OUTBOX_EVENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "outbox_event_id": str(self.outbox_event_id),
            "tenant_reference": self.tenant_reference,
            "event_type_code": self.event_type_code,
            "source_id": str(self.source_id),
            "payload_hash": self.payload_hash,
            "occurred_at": _format_enqueued_at(self.occurred_at),
            "enqueued_at": _format_enqueued_at(self.enqueued_at),
            "delivery_status": self.delivery_status,
            "attempted_delivery_count": self.attempted_delivery_count,
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "outbox_event_id": str(self.outbox_event_id),
            "event_type_code": self.event_type_code,
            "delivery_status": self.delivery_status,
            "enqueued_at": _format_enqueued_at(self.enqueued_at),
            "attempted_delivery_count": self.attempted_delivery_count,
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class WebhookOutboxEventPresentmentPage:
    """One tenant-scoped page of webhook-outbox-event metadata summaries."""

    webhook_outbox_events: tuple[WebhookOutboxEventPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{webhook_outbox_events, next_cursor}`` with summaries."""
        return {
            "webhook_outbox_events": [
                item.as_summary_dict() for item in self.webhook_outbox_events
            ],
            "next_cursor": self.next_cursor,
        }


class WebhookOutboxEventPresentmentService:
    """Read-only projector of stored webhook_outbox_event rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_webhook_outbox_event(
        self, tenant_reference: str, outbox_event_id: UUID
    ) -> WebhookOutboxEventPresentmentResult:
        """Return one same-tenant stored outbox event, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not publish, send, retry, or mark delivered.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_webhook_outbox_event(outbox_event_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise WebhookOutboxEventPresentmentQueryError("webhook_outbox_event_not_found")
        return self._project_event(tenant.tenant_reference, stored)

    def list_webhook_outbox_events(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> WebhookOutboxEventPresentmentPage:
        """Return one tenant page of outbox summaries without delivering.

        Order is ``enqueued_at`` then ``outbox_event_id``.
        The envelope is ``webhook_outbox_events`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_webhook_outbox_events_for_tenant(tenant.tenant_account_id),
            key=lambda event: (event.enqueued_at, event.outbox_event_id),
        )
        matched: list[StoredWebhookOutboxEvent] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.enqueued_at,
                stored.outbox_event_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.enqueued_at, last.outbox_event_id)
        return WebhookOutboxEventPresentmentPage(
            webhook_outbox_events=tuple(
                self._project_event(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise WebhookOutboxEventPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_event(
        self, tenant_reference: str, stored: StoredWebhookOutboxEvent
    ) -> WebhookOutboxEventPresentmentResult:
        """Project one stored outbox event using only persisted metadata."""
        attempted_delivery_count = len(
            self.ledger.list_webhook_delivery_attempts(stored.outbox_event_id)
        )
        return WebhookOutboxEventPresentmentResult(
            outbox_event_id=stored.outbox_event_id,
            tenant_reference=tenant_reference,
            event_type_code=stored.event_type_code,
            source_id=stored.source_id,
            payload_hash=stored.payload_hash,
            occurred_at=stored.occurred_at,
            enqueued_at=stored.enqueued_at,
            delivery_status=stored.delivery_status,
            attempted_delivery_count=attempted_delivery_count,
            next_operator_action=next_operator_action(
                delivery_status=stored.delivery_status
            ),
        )


def _format_enqueued_at(enqueued_at: datetime) -> str:
    """Render an enqueue timestamp as a timezone-aware ISO 8601 instant."""
    return enqueued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise WebhookOutboxEventPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise WebhookOutboxEventPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise WebhookOutboxEventPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(enqueued_at: datetime, outbox_event_id: UUID) -> str:
    """Encode the keyset cursor as enqueued_at then outbox event id."""
    return f"{_format_enqueued_at(enqueued_at)}|{outbox_event_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        enqueued_text, event_text = cursor.split("|", 1)
        return parse_iso8601_datetime(enqueued_text), UUID(event_text)
    except (TypeError, ValueError) as error:
        raise WebhookOutboxEventPresentmentQueryError("request_invalid") from error
