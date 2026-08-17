"""Tenant-scoped webhook-subscription presentment from stored metadata.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``webhook_subscription``.
3. Project identity, callback URL, event types, status, timestamps, and
   the next action.
4. Return metadata.  Do not mint, revoke, or reconstruct a secret.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import WebhookSubscriptionPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredWebhookSubscription


WEBHOOK_SUBSCRIPTION_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_RUN_DELIVERIES = "run_deliveries"
OPERATOR_ACTION_REGISTER = "register"


def next_operator_action(*, subscription_status: str) -> str:
    """Return run_deliveries while active, otherwise register a replacement."""
    if subscription_status == "active":
        return OPERATOR_ACTION_RUN_DELIVERIES
    if subscription_status == "revoked":
        return OPERATOR_ACTION_REGISTER
    raise WebhookSubscriptionPresentmentQueryError("request_invalid")


@dataclass(frozen=True)
class WebhookSubscriptionPresentmentResult:
    """Buyer-facing projection of one stored webhook subscription."""

    webhook_subscription_id: UUID
    tenant_reference: str
    callback_url: str
    event_type_codes: tuple[str, ...]
    subscription_status: str
    webhook_subscription_contract_version: int
    issued_at: datetime
    revoked_at: datetime | None
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "webhook_subscription_presentment_contract_version": (
                WEBHOOK_SUBSCRIPTION_PRESENTMENT_CONTRACT_VERSION
            ),
            "webhook_subscription_id": str(self.webhook_subscription_id),
            "tenant_reference": self.tenant_reference,
            "callback_url": self.callback_url,
            "event_type_codes": list(self.event_type_codes),
            "subscription_status": self.subscription_status,
            "webhook_subscription_contract_version": (
                self.webhook_subscription_contract_version
            ),
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = _format_issued_at(self.revoked_at)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "webhook_subscription_id": str(self.webhook_subscription_id),
            "callback_url": self.callback_url,
            "event_type_codes": list(self.event_type_codes),
            "subscription_status": self.subscription_status,
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class WebhookSubscriptionPresentmentPage:
    """One tenant-scoped page of webhook-subscription metadata summaries."""

    webhook_subscriptions: tuple[WebhookSubscriptionPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{webhook_subscriptions, next_cursor}`` with summaries."""
        return {
            "webhook_subscriptions": [
                item.as_summary_dict() for item in self.webhook_subscriptions
            ],
            "next_cursor": self.next_cursor,
        }


class WebhookSubscriptionPresentmentService:
    """Read-only projector of stored webhook-subscription metadata."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_webhook_subscription(
        self, tenant_reference: str, webhook_subscription_id: UUID
    ) -> WebhookSubscriptionPresentmentResult:
        """Return one same-tenant stored subscription, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not mint, revoke, or reconstruct a secret.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_webhook_subscription(webhook_subscription_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise WebhookSubscriptionPresentmentQueryError("webhook_subscription_not_found")
        return self._project_subscription(tenant.tenant_reference, stored)

    def list_webhook_subscriptions(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> WebhookSubscriptionPresentmentPage:
        """Return one tenant page of subscription summaries without secrets.

        Order is ``issued_at`` then ``webhook_subscription_id``.
        The envelope is ``webhook_subscriptions`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_webhook_subscriptions(tenant.tenant_account_id),
            key=lambda subscription: (
                subscription.issued_at,
                subscription.webhook_subscription_id,
            ),
        )
        matched: list[StoredWebhookSubscription] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.issued_at,
                stored.webhook_subscription_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.issued_at, last.webhook_subscription_id)
        return WebhookSubscriptionPresentmentPage(
            webhook_subscriptions=tuple(
                self._project_subscription(tenant.tenant_reference, stored)
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise WebhookSubscriptionPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_subscription(
        self, tenant_reference: str, stored: StoredWebhookSubscription
    ) -> WebhookSubscriptionPresentmentResult:
        """Project one stored subscription using only persisted metadata."""
        return WebhookSubscriptionPresentmentResult(
            webhook_subscription_id=stored.webhook_subscription_id,
            tenant_reference=tenant_reference,
            callback_url=stored.callback_url,
            event_type_codes=_event_type_codes(stored.event_type_set),
            subscription_status=stored.subscription_status,
            webhook_subscription_contract_version=stored.webhook_subscription_contract_version,
            issued_at=stored.issued_at,
            revoked_at=stored.revoked_at,
            next_operator_action=next_operator_action(
                subscription_status=stored.subscription_status
            ),
        )


def _event_type_codes(event_type_set: str) -> tuple[str, ...]:
    """Split the stored comma-separated event-type identity into codes."""
    return tuple(code for code in event_type_set.split(",") if code)


def _format_issued_at(issued_at: datetime) -> str:
    """Render an issue timestamp as a timezone-aware ISO 8601 instant."""
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise WebhookSubscriptionPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise WebhookSubscriptionPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise WebhookSubscriptionPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(issued_at: datetime, webhook_subscription_id: UUID) -> str:
    """Encode the keyset cursor as issued_at then subscription id."""
    return f"{_format_issued_at(issued_at)}|{webhook_subscription_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        issued_text, subscription_text = cursor.split("|", 1)
        return parse_iso8601_datetime(issued_text), UUID(subscription_text)
    except (TypeError, ValueError) as error:
        raise WebhookSubscriptionPresentmentQueryError("request_invalid") from error
