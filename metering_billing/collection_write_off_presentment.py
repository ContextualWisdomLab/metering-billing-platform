"""Tenant-scoped collection-write-off presentment from stored write-off rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_write_off``.
3. Project identity, write-off amount, and current remaining outstanding.
4. Return the statement.  Do not re-write-off, settle, capture payment, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.collection_write_off import OPERATOR_ACTION_SETTLE
from metering_billing.errors import (
    CollectionWriteOffPresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionWriteOff


COLLECTION_WRITE_OFF_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CollectionWriteOffPresentmentResult:
    """Buyer-facing projection of one stored collection write-off."""

    collection_write_off_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    write_off_amount: Decimal
    remaining_outstanding_amount: Decimal
    collection_write_off_status: str
    collection_case_status: str
    written_off_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "collection_write_off_presentment_contract_version": (
                COLLECTION_WRITE_OFF_PRESENTMENT_CONTRACT_VERSION
            ),
            "collection_write_off_id": str(self.collection_write_off_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "write_off_amount": format_exact_decimal(self.write_off_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_write_off_status": self.collection_write_off_status,
            "collection_case_status": self.collection_case_status,
            "written_off_at": _format_written_off_at(self.written_off_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "collection_write_off_id": str(self.collection_write_off_id),
            "collection_case_id": str(self.collection_case_id),
            "write_off_amount": format_exact_decimal(self.write_off_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "written_off_at": _format_written_off_at(self.written_off_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CollectionWriteOffPresentmentPage:
    """One tenant-scoped page of collection-write-off summaries."""

    collection_write_offs: tuple[CollectionWriteOffPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{collection_write_offs, next_cursor}`` with summaries."""
        return {
            "collection_write_offs": [
                item.as_summary_dict() for item in self.collection_write_offs
            ],
            "next_cursor": self.next_cursor,
        }


class CollectionWriteOffPresentmentService:
    """Read-only projector of stored collection_write_off rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_collection_write_off(
        self, tenant_reference: str, collection_write_off_id: UUID
    ) -> CollectionWriteOffPresentmentResult:
        """Return one same-tenant stored write-off, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not re-write-off, settle, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_collection_write_off(collection_write_off_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise CollectionWriteOffPresentmentQueryError("collection_write_off_not_found")
        return self._project_write_off(tenant.tenant_reference, stored)

    def list_collection_write_offs(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CollectionWriteOffPresentmentPage:
        """Return one tenant page of write-off summaries without re-writing-off.

        Order is ``written_off_at`` then ``collection_write_off_id``.
        The envelope is ``collection_write_offs`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_collection_write_offs_for_tenant(tenant.tenant_account_id),
            key=lambda write_off: (
                write_off.written_off_at,
                write_off.collection_write_off_id,
            ),
        )
        matched: list[StoredCollectionWriteOff] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.written_off_at,
                stored.collection_write_off_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.written_off_at, last.collection_write_off_id)
        return CollectionWriteOffPresentmentPage(
            collection_write_offs=tuple(
                self._project_write_off(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionWriteOffPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_write_off(
        self, tenant_reference: str, stored: StoredCollectionWriteOff
    ) -> CollectionWriteOffPresentmentResult:
        """Project one stored write-off using the current collection status."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None:
            raise CollectionWriteOffPresentmentQueryError("collection_write_off_not_found")
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        return CollectionWriteOffPresentmentResult(
            collection_write_off_id=stored.collection_write_off_id,
            tenant_reference=tenant_reference,
            collection_case_id=stored.collection_case_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            write_off_amount=stored.write_off_amount,
            remaining_outstanding_amount=remaining,
            collection_write_off_status=stored.collection_write_off_status,
            collection_case_status=collection_case.collection_case_status,
            written_off_at=stored.written_off_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_SETTLE,
        )


def _format_written_off_at(written_off_at: datetime) -> str:
    """Render a write-off timestamp as a timezone-aware ISO 8601 instant."""
    return written_off_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CollectionWriteOffPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CollectionWriteOffPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CollectionWriteOffPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(written_off_at: datetime, collection_write_off_id: UUID) -> str:
    """Encode the keyset cursor as written_off_at then write-off id."""
    return f"{_format_written_off_at(written_off_at)}|{collection_write_off_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        written_text, write_off_text = cursor.split("|", 1)
        return parse_iso8601_datetime(written_text), UUID(write_off_text)
    except (TypeError, ValueError) as error:
        raise CollectionWriteOffPresentmentQueryError("request_invalid") from error
