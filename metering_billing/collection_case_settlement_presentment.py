"""Tenant-scoped collection-case-settlement presentment from stored settle rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_case_settlement``.
3. Project identity, zero remaining, and current case status.
4. Return the statement.  Do not re-settle, capture payment, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.collection_case_settlement import OPERATOR_ACTION_WAIT
from metering_billing.errors import (
    CollectionCaseSettlementPresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionCaseSettlement


COLLECTION_CASE_SETTLEMENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CollectionCaseSettlementPresentmentResult:
    """Buyer-facing projection of one stored collection-case settlement."""

    collection_case_settlement_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    remaining_outstanding_amount: Decimal
    collection_case_settlement_status: str
    collection_case_status: str
    settled_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "collection_case_settlement_presentment_contract_version": (
                COLLECTION_CASE_SETTLEMENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "collection_case_settlement_id": str(self.collection_case_settlement_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_case_settlement_status": self.collection_case_settlement_status,
            "collection_case_status": self.collection_case_status,
            "settled_at": _format_settled_at(self.settled_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "collection_case_settlement_id": str(self.collection_case_settlement_id),
            "collection_case_id": str(self.collection_case_id),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "settled_at": _format_settled_at(self.settled_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CollectionCaseSettlementPresentmentPage:
    """One tenant-scoped page of collection-case-settlement summaries."""

    collection_case_settlements: tuple[CollectionCaseSettlementPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{collection_case_settlements, next_cursor}`` with summaries."""
        return {
            "collection_case_settlements": [
                item.as_summary_dict() for item in self.collection_case_settlements
            ],
            "next_cursor": self.next_cursor,
        }


class CollectionCaseSettlementPresentmentService:
    """Read-only projector of stored collection_case_settlement rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_collection_case_settlement(
        self, tenant_reference: str, collection_case_settlement_id: UUID
    ) -> CollectionCaseSettlementPresentmentResult:
        """Return one same-tenant stored settlement, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not re-settle, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_collection_case_settlement(collection_case_settlement_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise CollectionCaseSettlementPresentmentQueryError(
                "collection_case_settlement_not_found"
            )
        return self._project_settlement(tenant.tenant_reference, stored)

    def list_collection_case_settlements(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CollectionCaseSettlementPresentmentPage:
        """Return one tenant page of settlement summaries without re-settling.

        Order is ``settled_at`` then ``collection_case_settlement_id``.
        The envelope is ``collection_case_settlements`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_collection_case_settlements_for_tenant(tenant.tenant_account_id),
            key=lambda settlement: (
                settlement.settled_at,
                settlement.collection_case_settlement_id,
            ),
        )
        matched: list[StoredCollectionCaseSettlement] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.settled_at,
                stored.collection_case_settlement_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(
                last.settled_at, last.collection_case_settlement_id
            )
        return CollectionCaseSettlementPresentmentPage(
            collection_case_settlements=tuple(
                self._project_settlement(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionCaseSettlementPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_settlement(
        self, tenant_reference: str, stored: StoredCollectionCaseSettlement
    ) -> CollectionCaseSettlementPresentmentResult:
        """Project one stored settlement using the current collection status."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None:
            raise CollectionCaseSettlementPresentmentQueryError(
                "collection_case_settlement_not_found"
            )
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        return CollectionCaseSettlementPresentmentResult(
            collection_case_settlement_id=stored.collection_case_settlement_id,
            tenant_reference=tenant_reference,
            collection_case_id=stored.collection_case_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            remaining_outstanding_amount=remaining,
            collection_case_settlement_status=stored.collection_case_settlement_status,
            collection_case_status=collection_case.collection_case_status,
            settled_at=stored.settled_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_settled_at(settled_at: datetime) -> str:
    """Render a settle timestamp as a timezone-aware ISO 8601 instant."""
    return settled_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CollectionCaseSettlementPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CollectionCaseSettlementPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CollectionCaseSettlementPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(settled_at: datetime, collection_case_settlement_id: UUID) -> str:
    """Encode the keyset cursor as settled_at then settlement id."""
    return f"{_format_settled_at(settled_at)}|{collection_case_settlement_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        settled_text, settlement_text = cursor.split("|", 1)
        return parse_iso8601_datetime(settled_text), UUID(settlement_text)
    except (TypeError, ValueError) as error:
        raise CollectionCaseSettlementPresentmentQueryError("request_invalid") from error
