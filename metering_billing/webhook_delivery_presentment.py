"""Tenant-scoped webhook-delivery presentment from stored attempt facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``webhook_delivery_attempt``.
3. Project identity, event type, attempt outcome, and the next action.
4. Return the attempt.  Do not resend, retry, or invent delivery status.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import WebhookDeliveryPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredWebhookDeliveryAttempt


WEBHOOK_DELIVERY_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_RUN_DELIVERIES = "run_deliveries"


def next_operator_action(*, delivered_at: datetime | None) -> str:
    """Return wait after a stored success, otherwise run_deliveries."""
    if delivered_at is not None:
        return OPERATOR_ACTION_WAIT
    return OPERATOR_ACTION_RUN_DELIVERIES


@dataclass(frozen=True)
class WebhookDeliveryPresentmentResult:
    """Buyer-facing projection of one stored webhook delivery attempt."""

    delivery_attempt_id: UUID
    tenant_reference: str
    webhook_subscription_id: UUID
    outbox_event_id: UUID
    event_type_code: str
    source_id: UUID
    attempt_number: int
    http_status: int | None
    failure_reason_code: str | None
    attempted_at: datetime
    delivered_at: datetime | None
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "webhook_delivery_presentment_contract_version": (
                WEBHOOK_DELIVERY_PRESENTMENT_CONTRACT_VERSION
            ),
            "delivery_attempt_id": str(self.delivery_attempt_id),
            "tenant_reference": self.tenant_reference,
            "webhook_subscription_id": str(self.webhook_subscription_id),
            "outbox_event_id": str(self.outbox_event_id),
            "event_type_code": self.event_type_code,
            "source_id": str(self.source_id),
            "attempt_number": self.attempt_number,
            "attempted_at": _format_attempted_at(self.attempted_at),
            "next_operator_action": self.next_operator_action,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.failure_reason_code is not None:
            payload["failure_reason_code"] = self.failure_reason_code
        if self.delivered_at is not None:
            payload["delivered_at"] = _format_attempted_at(self.delivered_at)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "delivery_attempt_id": str(self.delivery_attempt_id),
            "webhook_subscription_id": str(self.webhook_subscription_id),
            "event_type_code": self.event_type_code,
            "attempt_number": self.attempt_number,
            "attempted_at": _format_attempted_at(self.attempted_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class WebhookDeliveryPresentmentPage:
    """One tenant-scoped page of webhook-delivery summaries."""

    webhook_deliveries: tuple[WebhookDeliveryPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{webhook_deliveries, next_cursor}`` with summaries."""
        return {
            "webhook_deliveries": [item.as_summary_dict() for item in self.webhook_deliveries],
            "next_cursor": self.next_cursor,
        }


class WebhookDeliveryPresentmentService:
    """Read-only projector of stored webhook delivery attempts."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_webhook_delivery(
        self, tenant_reference: str, delivery_attempt_id: UUID
    ) -> WebhookDeliveryPresentmentResult:
        """Return one same-tenant stored attempt, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not resend, retry, or invent ``delivery_status``.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_webhook_delivery_attempt(delivery_attempt_id)
        if stored is None:
            raise WebhookDeliveryPresentmentQueryError("webhook_delivery_not_found")
        return self._require_projected(
            tenant.tenant_reference, tenant.tenant_account_id, stored
        )

    def list_webhook_deliveries(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> WebhookDeliveryPresentmentPage:
        """Return one tenant page of delivery summaries without resending.

        Order is ``attempted_at`` then ``delivery_attempt_id``.
        The envelope is ``webhook_deliveries`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_webhook_delivery_attempts_for_tenant(tenant.tenant_account_id),
            key=lambda attempt: (attempt.attempted_at, attempt.delivery_attempt_id),
        )
        matched: list[StoredWebhookDeliveryAttempt] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.attempted_at,
                stored.delivery_attempt_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.attempted_at, last.delivery_attempt_id)
        return WebhookDeliveryPresentmentPage(
            webhook_deliveries=tuple(
                self._require_projected(
                    tenant.tenant_reference, tenant.tenant_account_id, stored
                )
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise WebhookDeliveryPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _require_projected(
        self,
        tenant_reference: str,
        tenant_account_id: UUID,
        stored: StoredWebhookDeliveryAttempt,
    ) -> WebhookDeliveryPresentmentResult:
        """Project one same-tenant attempt or fail closed without leaking."""
        projected = self._project_attempt(tenant_reference, tenant_account_id, stored)
        if projected is None:
            raise WebhookDeliveryPresentmentQueryError("webhook_delivery_not_found")
        return projected

    def _project_attempt(
        self,
        tenant_reference: str,
        tenant_account_id: UUID,
        stored: StoredWebhookDeliveryAttempt,
    ) -> WebhookDeliveryPresentmentResult | None:
        """Project one stored attempt using only persisted commercial fields."""
        outbox = self.ledger.get_webhook_outbox_event(stored.outbox_event_id)
        if outbox is None or outbox.tenant_account_id != tenant_account_id:
            return None
        return WebhookDeliveryPresentmentResult(
            delivery_attempt_id=stored.delivery_attempt_id,
            tenant_reference=tenant_reference,
            webhook_subscription_id=stored.webhook_subscription_id,
            outbox_event_id=stored.outbox_event_id,
            event_type_code=outbox.event_type_code,
            source_id=outbox.source_id,
            attempt_number=stored.attempt_number,
            http_status=stored.http_status,
            failure_reason_code=stored.failure_reason_code,
            attempted_at=stored.attempted_at,
            delivered_at=stored.delivered_at,
            next_operator_action=next_operator_action(delivered_at=stored.delivered_at),
        )


def _format_attempted_at(attempted_at: datetime) -> str:
    """Render an attempt timestamp as a timezone-aware ISO 8601 instant."""
    return attempted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise WebhookDeliveryPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise WebhookDeliveryPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise WebhookDeliveryPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(attempted_at: datetime, delivery_attempt_id: UUID) -> str:
    """Encode the keyset cursor as attempted_at then delivery attempt id."""
    return f"{_format_attempted_at(attempted_at)}|{delivery_attempt_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        attempted_text, attempt_text = cursor.split("|", 1)
        return parse_iso8601_datetime(attempted_text), UUID(attempt_text)
    except (TypeError, ValueError) as error:
        raise WebhookDeliveryPresentmentQueryError("request_invalid") from error
