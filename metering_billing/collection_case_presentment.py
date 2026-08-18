"""Tenant-scoped collection-case presentment projected from stored commercial facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_case`` and dunning rows.
3. Project outstanding, commercial status, last/next dunning, and next action.
4. Return the case.  Do not capture, credit, post, or call AIS.

IFRS 15 treats the case as presentation of remaining consideration, not proof
that revenue has been collected (IFRS Foundation, 2024).  RFC 9110 treats GET
as a safe, idempotent read (Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_SETTLED_STATUS,
    COLLECTION_CASE_VOIDED_STATUS,
    CollectionDunningEventResult,
    _derived_collection_case_status,
)
from metering_billing.errors import CollectionCasePresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionCase


COLLECTION_CASE_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
ZERO = Decimal("0")
FIRST_NOTICE_CODE = "first_notice"
OVERDUE_NOTICE_CODE = "overdue_notice"
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_COLLECT = "collect"
OPERATOR_ACTION_CREDIT = "credit"


def next_operator_action(
    collection_case_status: str, outstanding: Decimal, credited_amount: Decimal
) -> str:
    """Return collect, credit, or wait from stored commercial facts only.

    Settled or zero outstanding waits.  An open case that already has an
    accepted credit still has adjustable consideration, so the next action is
    credit.  Otherwise the operator collects.
    """
    if collection_case_status in {
        COLLECTION_CASE_SETTLED_STATUS,
        COLLECTION_CASE_VOIDED_STATUS,
    } or outstanding <= ZERO:
        return OPERATOR_ACTION_WAIT
    if collection_case_status == "open" and credited_amount > ZERO:
        return OPERATOR_ACTION_CREDIT
    return OPERATOR_ACTION_COLLECT


@dataclass(frozen=True)
class CollectionCasePresentmentResult:
    """Buyer-facing projection of one stored collection case."""

    collection_case_id: UUID
    tenant_reference: str
    invoice_draft_id: UUID
    currency_code: str
    collection_outstanding: Decimal
    collection_case_status: str
    opened_at: datetime
    next_operator_action: str
    last_dunning_notice_code: str | None
    next_dunning_notice_code: str | None
    dunning_events: tuple[CollectionDunningEventResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "collection_case_presentment_contract_version": (
                COLLECTION_CASE_PRESENTMENT_CONTRACT_VERSION
            ),
            "collection_case_id": str(self.collection_case_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "collection_outstanding": format_exact_decimal(self.collection_outstanding),
            "collection_case_status": self.collection_case_status,
            "opened_at": _format_opened_at(self.opened_at),
            "next_operator_action": self.next_operator_action,
            "dunning_events": [event.as_contract_dict() for event in self.dunning_events],
        }
        if self.last_dunning_notice_code is not None:
            payload["last_dunning_notice_code"] = self.last_dunning_notice_code
        if self.next_dunning_notice_code is not None:
            payload["next_dunning_notice_code"] = self.next_dunning_notice_code
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/collection-cases``."""
        return {
            "collection_case_id": str(self.collection_case_id),
            "collection_outstanding": format_exact_decimal(self.collection_outstanding),
            "currency_code": self.currency_code,
            "collection_case_status": self.collection_case_status,
            "opened_at": _format_opened_at(self.opened_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CollectionCasePresentmentPage:
    """One tenant-scoped page of collection-case summaries."""

    collection_cases: tuple[CollectionCasePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{collection_cases, next_cursor}`` with summary items."""
        return {
            "collection_cases": [item.as_summary_dict() for item in self.collection_cases],
            "next_cursor": self.next_cursor,
        }


class CollectionCasePresentmentService:
    """Read-only projector of stored collection cases into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_collection_case(
        self, tenant_reference: str, collection_case_id: UUID
    ) -> CollectionCasePresentmentResult:
        """Return one same-tenant case, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change case, dunning, draft, or proposal status.
        """
        tenant = self._require_tenant(tenant_reference)
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            raise CollectionCasePresentmentQueryError("collection_case_not_found")
        return self._project_case(tenant.tenant_reference, collection_case)

    def list_collection_cases(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CollectionCasePresentmentPage:
        """Return one tenant page of case summaries without mutating cases.

        Order is ``opened_at`` then ``collection_case_id``.  The envelope is
        ``collection_cases`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_collection_cases(tenant.tenant_account_id),
            key=lambda case: (case.opened_at, case.collection_case_id),
        )
        matched: list[StoredCollectionCase] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.opened_at, stored.collection_case_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.opened_at, last.collection_case_id)
        return CollectionCasePresentmentPage(
            collection_cases=tuple(
                self._project_case(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionCasePresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_case(
        self, tenant_reference: str, collection_case: StoredCollectionCase
    ) -> CollectionCasePresentmentResult:
        """Project one stored case plus dunning history and next action."""
        stored_events = self.ledger.list_collection_dunning_events(
            collection_case.collection_case_id
        )
        collection_case_status = _derived_collection_case_status(collection_case, stored_events)
        outstanding = parse_invoice_amount(collection_case.outstanding_amount)
        credited_amount = self._credited_amount(
            collection_case.tenant_account_id, collection_case.invoice_draft_id
        )
        dunning_events = tuple(
            CollectionDunningEventResult(
                dunning_event_id=event.collection_dunning_event_id,
                dunning_event_number=event.dunning_event_number,
                dunning_notice_code=event.dunning_notice_code,
                occurred_at=event.occurred_at,
            )
            for event in stored_events
        )
        last_notice = dunning_events[-1].dunning_notice_code if dunning_events else None
        return CollectionCasePresentmentResult(
            collection_case_id=collection_case.collection_case_id,
            tenant_reference=tenant_reference,
            invoice_draft_id=collection_case.invoice_draft_id,
            currency_code=collection_case.currency_code,
            collection_outstanding=outstanding,
            collection_case_status=collection_case_status,
            opened_at=collection_case.opened_at,
            next_operator_action=next_operator_action(
                collection_case_status, outstanding, credited_amount
            ),
            last_dunning_notice_code=last_notice,
            next_dunning_notice_code=_next_dunning_notice_code(
                collection_case_status, last_notice
            ),
            dunning_events=dunning_events,
        )

    def _credited_amount(self, tenant_account_id: UUID, invoice_draft_id: UUID) -> Decimal:
        """Sum accepted credits for one tenant draft as an exact decimal."""
        credited = ZERO
        for credit in self.ledger.list_credit_adjustments(tenant_account_id):
            if credit.invoice_draft_id != invoice_draft_id:
                continue
            credited += parse_invoice_amount(credit.credit_amount)
        return credited


def _next_dunning_notice_code(collection_case_status: str, last_notice: str | None) -> str | None:
    """Return the next stored notice code, or ``None`` when no further notice exists."""
    if collection_case_status in {
        COLLECTION_CASE_SETTLED_STATUS,
        COLLECTION_CASE_VOIDED_STATUS,
    }:
        return None
    if last_notice is None:
        return FIRST_NOTICE_CODE
    if last_notice == FIRST_NOTICE_CODE:
        return OVERDUE_NOTICE_CODE
    return None


def _format_opened_at(opened_at: datetime) -> str:
    """Render ``opened_at`` as a timezone-aware ISO 8601 instant."""
    return opened_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CollectionCasePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CollectionCasePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CollectionCasePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(opened_at: datetime, collection_case_id: UUID) -> str:
    """Encode the keyset cursor as opened_at then collection_case_id."""
    return f"{_format_opened_at(opened_at)}|{collection_case_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        opened_text, case_text = cursor.split("|", 1)
        return parse_iso8601_datetime(opened_text), UUID(case_text)
    except (TypeError, ValueError) as error:
        raise CollectionCasePresentmentQueryError("request_invalid") from error
