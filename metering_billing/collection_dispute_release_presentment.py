"""Tenant-scoped collection-dispute release presentment from stored rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_dispute`` whose status is ``released``.
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

from metering_billing.collection_dispute_release import (
    COLLECTION_DISPUTE_RELEASED_STATUS,
    OPERATOR_ACTION_WAIT,
)
from metering_billing.errors import (
    CollectionDisputeReleasePresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionDispute


COLLECTION_DISPUTE_RELEASE_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CollectionDisputeReleasePresentmentResult:
    """Buyer-facing projection of one released collection dispute."""

    collection_dispute_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    remaining_outstanding_amount: Decimal
    collection_dispute_status: str
    collection_case_status: str
    released_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "collection_dispute_release_presentment_contract_version": (
                COLLECTION_DISPUTE_RELEASE_PRESENTMENT_CONTRACT_VERSION
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
            "released_at": _format_released_at(self.released_at),
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
            "released_at": _format_released_at(self.released_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CollectionDisputeReleasePresentmentPage:
    """One tenant-scoped page of collection-dispute release summaries."""

    collection_dispute_releases: tuple[CollectionDisputeReleasePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{collection_dispute_releases, next_cursor}`` with summaries."""
        return {
            "collection_dispute_releases": [
                item.as_summary_dict() for item in self.collection_dispute_releases
            ],
            "next_cursor": self.next_cursor,
        }


class CollectionDisputeReleasePresentmentService:
    """Read-only projector of released collection_dispute rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_collection_dispute_release(
        self, tenant_reference: str, collection_dispute_id: UUID
    ) -> CollectionDisputeReleasePresentmentResult:
        """Return one same-tenant stored release, or fail closed.

        A missing, still-held, or cross-tenant identifier is
        indistinguishable.  The read does not re-hold, release, capture
        payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_collection_dispute(collection_dispute_id)
        if (
            stored is None
            or stored.tenant_account_id != tenant.tenant_account_id
            or stored.collection_dispute_status != COLLECTION_DISPUTE_RELEASED_STATUS
        ):
            raise CollectionDisputeReleasePresentmentQueryError(
                "collection_dispute_release_not_found"
            )
        return self._project_release(tenant.tenant_reference, stored)

    def list_collection_dispute_releases(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CollectionDisputeReleasePresentmentPage:
        """Return one tenant page of released-dispute summaries.

        Order is ``released_at`` then ``collection_dispute_id``.
        The envelope is ``collection_dispute_releases`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            (
                dispute
                for dispute in self.ledger.list_collection_disputes_for_tenant(
                    tenant.tenant_account_id
                )
                if dispute.collection_dispute_status == COLLECTION_DISPUTE_RELEASED_STATUS
                and dispute.released_at is not None
            ),
            key=lambda dispute: (
                dispute.released_at,
                dispute.collection_dispute_id,
            ),
        )
        matched: list[StoredCollectionDispute] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.released_at,
                stored.collection_dispute_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.released_at, last.collection_dispute_id)
        return CollectionDisputeReleasePresentmentPage(
            collection_dispute_releases=tuple(
                self._project_release(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionDisputeReleasePresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_release(
        self, tenant_reference: str, stored: StoredCollectionDispute
    ) -> CollectionDisputeReleasePresentmentResult:
        """Project one stored release using the current collection status."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None or stored.released_at is None:
            raise CollectionDisputeReleasePresentmentQueryError(
                "collection_dispute_release_not_found"
            )
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        return CollectionDisputeReleasePresentmentResult(
            collection_dispute_id=stored.collection_dispute_id,
            tenant_reference=tenant_reference,
            collection_case_id=stored.collection_case_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            remaining_outstanding_amount=remaining,
            collection_dispute_status=stored.collection_dispute_status,
            collection_case_status=collection_case.collection_case_status,
            released_at=stored.released_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_released_at(released_at: datetime) -> str:
    """Render a release timestamp as a timezone-aware ISO 8601 instant."""
    return released_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CollectionDisputeReleasePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CollectionDisputeReleasePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CollectionDisputeReleasePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(released_at: datetime, collection_dispute_id: UUID) -> str:
    """Encode the keyset cursor as released_at then dispute id."""
    return f"{_format_released_at(released_at)}|{collection_dispute_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        released_text, dispute_text = cursor.split("|", 1)
        return parse_iso8601_datetime(released_text), UUID(dispute_text)
    except (TypeError, ValueError) as error:
        raise CollectionDisputeReleasePresentmentQueryError("request_invalid") from error
