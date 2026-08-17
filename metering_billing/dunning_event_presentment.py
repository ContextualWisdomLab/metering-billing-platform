"""Tenant-scoped dunning-event presentment from stored collection_dunning_event rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_dunning_event``.
3. Project identity, case, sequence, notice code, timestamp, and next action.
4. Return stored facts.  Do not send mail, SMS, or capture payment.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.collection_case import _derived_collection_case_status
from metering_billing.errors import DunningEventPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionDunningEvent


DUNNING_EVENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_COLLECT = "collect"


def next_operator_action(*, collection_case_status: str) -> str:
    """Return wait when the case is settled, otherwise collect.

    The next action stays on the #10/#26 case workflow.  It does not invent
    a send, email, or SMS command.
    """
    if collection_case_status == "settled":
        return OPERATOR_ACTION_WAIT
    if collection_case_status in {"open", "dunning"}:
        return OPERATOR_ACTION_COLLECT
    raise DunningEventPresentmentQueryError("request_invalid")


@dataclass(frozen=True)
class DunningEventPresentmentResult:
    """Buyer-facing projection of one stored collection dunning event."""

    dunning_event_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    dunning_event_number: int
    dunning_notice_code: str
    occurred_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "dunning_event_presentment_contract_version": (
                DUNNING_EVENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "dunning_event_id": str(self.dunning_event_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "dunning_event_number": self.dunning_event_number,
            "dunning_notice_code": self.dunning_notice_code,
            "occurred_at": _format_occurred_at(self.occurred_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "dunning_event_id": str(self.dunning_event_id),
            "collection_case_id": str(self.collection_case_id),
            "dunning_event_number": self.dunning_event_number,
            "dunning_notice_code": self.dunning_notice_code,
            "occurred_at": _format_occurred_at(self.occurred_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class DunningEventPresentmentPage:
    """One tenant-scoped page of dunning-event metadata summaries."""

    dunning_events: tuple[DunningEventPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{dunning_events, next_cursor}`` with summaries."""
        return {
            "dunning_events": [item.as_summary_dict() for item in self.dunning_events],
            "next_cursor": self.next_cursor,
        }


class DunningEventPresentmentService:
    """Read-only projector of stored collection_dunning_event rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_dunning_event(
        self, tenant_reference: str, dunning_event_id: UUID
    ) -> DunningEventPresentmentResult:
        """Return one same-tenant stored reminder, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not send mail, SMS, or capture payment.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_collection_dunning_event(dunning_event_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise DunningEventPresentmentQueryError("dunning_event_not_found")
        return self._project_event(tenant.tenant_reference, stored)

    def list_dunning_events(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> DunningEventPresentmentPage:
        """Return one tenant page of reminder summaries without sending.

        Order is ``occurred_at`` then ``collection_dunning_event_id``.
        The envelope is ``dunning_events`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_collection_dunning_events_for_tenant(tenant.tenant_account_id),
            key=lambda event: (event.occurred_at, event.collection_dunning_event_id),
        )
        matched: list[StoredCollectionDunningEvent] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.occurred_at,
                stored.collection_dunning_event_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(
                last.occurred_at, last.collection_dunning_event_id
            )
        return DunningEventPresentmentPage(
            dunning_events=tuple(
                self._project_event(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise DunningEventPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_event(
        self, tenant_reference: str, stored: StoredCollectionDunningEvent
    ) -> DunningEventPresentmentResult:
        """Project one stored reminder using only persisted facts."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != stored.tenant_account_id
        ):
            raise DunningEventPresentmentQueryError("dunning_event_not_found")
        case_events = self.ledger.list_collection_dunning_events(stored.collection_case_id)
        collection_case_status = _derived_collection_case_status(collection_case, case_events)
        return DunningEventPresentmentResult(
            dunning_event_id=stored.collection_dunning_event_id,
            tenant_reference=tenant_reference,
            collection_case_id=stored.collection_case_id,
            dunning_event_number=stored.dunning_event_number,
            dunning_notice_code=stored.dunning_notice_code,
            occurred_at=stored.occurred_at,
            next_operator_action=next_operator_action(
                collection_case_status=collection_case_status
            ),
        )


def _format_occurred_at(occurred_at: datetime) -> str:
    """Render an occurrence timestamp as a timezone-aware ISO 8601 instant."""
    return occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise DunningEventPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise DunningEventPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise DunningEventPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(occurred_at: datetime, dunning_event_id: UUID) -> str:
    """Encode the keyset cursor as occurred_at then dunning event id."""
    return f"{_format_occurred_at(occurred_at)}|{dunning_event_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        occurred_text, event_text = cursor.split("|", 1)
        return parse_iso8601_datetime(occurred_text), UUID(event_text)
    except (TypeError, ValueError) as error:
        raise DunningEventPresentmentQueryError("request_invalid") from error
