"""Tenant-scoped collection-dispute presentment from stored hold rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_dispute``.
3. Project identity and current remaining outstanding.
4. Return the statement.  Do not re-hold, release, capture payment, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.collection_dispute import OPERATOR_ACTION_WAIT
from metering_billing.errors import (
    CollectionDisputePresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionDispute


COLLECTION_DISPUTE_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CollectionDisputePresentmentResult:
    """Buyer-facing projection of one stored collection dispute hold."""

    collection_dispute_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    remaining_outstanding_amount: Decimal
    collection_dispute_status: str
    collection_case_status: str
    held_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "collection_dispute_presentment_contract_version": (
                COLLECTION_DISPUTE_PRESENTMENT_CONTRACT_VERSION
            ),
            "collection_dispute_id": str(self.collection_dispute_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_dispute_status": self.collection_dispute_status,
            "collection_case_status": self.collection_case_status,
            "held_at": _format_held_at(self.held_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "collection_dispute_id": str(self.collection_dispute_id),
            "collection_case_id": str(self.collection_case_id),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "held_at": _format_held_at(self.held_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CollectionDisputePresentmentPage:
    """One tenant-scoped page of collection-dispute summaries."""

    collection_disputes: tuple[CollectionDisputePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{collection_disputes, next_cursor}`` with summaries."""
        return {
            "collection_disputes": [
                item.as_summary_dict() for item in self.collection_disputes
            ],
            "next_cursor": self.next_cursor,
        }


class CollectionDisputePresentmentService:
    """Read-only projector of stored collection_dispute rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_collection_dispute(
        self, tenant_reference: str, collection_dispute_id: UUID
    ) -> CollectionDisputePresentmentResult:
        """Return one same-tenant stored hold, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not re-hold, release, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_collection_dispute(collection_dispute_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise CollectionDisputePresentmentQueryError("collection_dispute_not_found")
        return self._project_dispute(tenant.tenant_reference, stored)

    def list_collection_disputes(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CollectionDisputePresentmentPage:
        """Return one tenant page of dispute-hold summaries without re-holding.

        Order is ``held_at`` then ``collection_dispute_id``.
        The envelope is ``collection_disputes`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_collection_disputes_for_tenant(tenant.tenant_account_id),
            key=lambda dispute: (
                dispute.held_at,
                dispute.collection_dispute_id,
            ),
        )
        matched: list[StoredCollectionDispute] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.held_at,
                stored.collection_dispute_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.held_at, last.collection_dispute_id)
        return CollectionDisputePresentmentPage(
            collection_disputes=tuple(
                self._project_dispute(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionDisputePresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_dispute(
        self, tenant_reference: str, stored: StoredCollectionDispute
    ) -> CollectionDisputePresentmentResult:
        """Project one stored hold using the current collection status."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None:
            raise CollectionDisputePresentmentQueryError("collection_dispute_not_found")
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        return CollectionDisputePresentmentResult(
            collection_dispute_id=stored.collection_dispute_id,
            tenant_reference=tenant_reference,
            collection_case_id=stored.collection_case_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            remaining_outstanding_amount=remaining,
            collection_dispute_status=stored.collection_dispute_status,
            collection_case_status=collection_case.collection_case_status,
            held_at=stored.held_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_held_at(held_at: datetime) -> str:
    """Render a hold timestamp as a timezone-aware ISO 8601 instant."""
    return held_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CollectionDisputePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CollectionDisputePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CollectionDisputePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(held_at: datetime, collection_dispute_id: UUID) -> str:
    """Encode the keyset cursor as held_at then dispute id."""
    return f"{_format_held_at(held_at)}|{collection_dispute_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        held_text, dispute_text = cursor.split("|", 1)
        return parse_iso8601_datetime(held_text), UUID(dispute_text)
    except (TypeError, ValueError) as error:
        raise CollectionDisputePresentmentQueryError("request_invalid") from error
