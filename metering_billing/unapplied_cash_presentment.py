"""Tenant-scoped unapplied-cash presentment from stored leftover rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``unapplied_cash``.
3. Project identity, leftover amount, and the source receipt.
4. Return the statement.  Do not apply leftover, capture, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import UnappliedCashPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.unapplied_cash import OPERATOR_ACTION_WAIT
from metering_billing.usage_ledger import MemoryUsageLedger, StoredUnappliedCash


UNAPPLIED_CASH_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class UnappliedCashPresentmentResult:
    """Buyer-facing projection of one stored unapplied-cash row."""

    unapplied_cash_id: UUID
    tenant_reference: str
    payment_receipt_id: UUID
    payment_intent_id: UUID
    collection_case_id: UUID
    currency_code: str
    unapplied_amount: Decimal
    received_amount: Decimal
    applied_amount: Decimal
    unapplied_cash_status: str
    parked_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "unapplied_cash_presentment_contract_version": (
                UNAPPLIED_CASH_PRESENTMENT_CONTRACT_VERSION
            ),
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "tenant_reference": self.tenant_reference,
            "payment_receipt_id": str(self.payment_receipt_id),
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "unapplied_amount": format_exact_decimal(self.unapplied_amount),
            "received_amount": format_exact_decimal(self.received_amount),
            "applied_amount": format_exact_decimal(self.applied_amount),
            "unapplied_cash_status": self.unapplied_cash_status,
            "parked_at": _format_parked_at(self.parked_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by leftover GET."""
        return {
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "unapplied_amount": format_exact_decimal(self.unapplied_amount),
            "parked_at": _format_parked_at(self.parked_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class UnappliedCashPresentmentPage:
    """One tenant-scoped page of unapplied-cash summaries."""

    unapplied_cash: tuple[UnappliedCashPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{unapplied_cash, next_cursor}`` with summaries."""
        return {
            "unapplied_cash": [item.as_summary_dict() for item in self.unapplied_cash],
            "next_cursor": self.next_cursor,
        }


class UnappliedCashPresentmentService:
    """Read-only projector of stored unapplied_cash rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_unapplied_cash(
        self, tenant_reference: str, unapplied_cash_id: UUID
    ) -> UnappliedCashPresentmentResult:
        """Return one same-tenant stored leftover, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The
        read does not apply leftover, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_unapplied_cash(unapplied_cash_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise UnappliedCashPresentmentQueryError("unapplied_cash_not_found")
        return self._project_unapplied_cash(tenant.tenant_reference, stored)

    def list_unapplied_cash(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> UnappliedCashPresentmentPage:
        """Return one tenant page of leftover summaries without applying them.

        Order is ``parked_at`` then ``unapplied_cash_id``.  The envelope is
        ``unapplied_cash`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_unapplied_cash_for_tenant(tenant.tenant_account_id),
            key=lambda parked: (parked.parked_at, parked.unapplied_cash_id),
        )
        matched: list[StoredUnappliedCash] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.parked_at,
                stored.unapplied_cash_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.parked_at, last.unapplied_cash_id)
        return UnappliedCashPresentmentPage(
            unapplied_cash=tuple(
                self._project_unapplied_cash(tenant.tenant_reference, stored)
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise UnappliedCashPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_unapplied_cash(
        self, tenant_reference: str, stored: StoredUnappliedCash
    ) -> UnappliedCashPresentmentResult:
        """Project one stored leftover without applying it to a case."""
        return UnappliedCashPresentmentResult(
            unapplied_cash_id=stored.unapplied_cash_id,
            tenant_reference=tenant_reference,
            payment_receipt_id=stored.payment_receipt_id,
            payment_intent_id=stored.payment_intent_id,
            collection_case_id=stored.collection_case_id,
            currency_code=stored.currency_code,
            unapplied_amount=stored.unapplied_amount,
            received_amount=stored.received_amount,
            applied_amount=stored.applied_amount,
            unapplied_cash_status=stored.unapplied_cash_status,
            parked_at=stored.parked_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_parked_at(parked_at: datetime) -> str:
    """Render a park timestamp as a timezone-aware ISO 8601 instant."""
    return parked_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise UnappliedCashPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise UnappliedCashPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise UnappliedCashPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(parked_at: datetime, unapplied_cash_id: UUID) -> str:
    """Encode the keyset cursor as parked_at then unapplied-cash id."""
    return f"{_format_parked_at(parked_at)}|{unapplied_cash_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        parked_text, leftover_text = cursor.split("|", 1)
        return parse_iso8601_datetime(parked_text), UUID(leftover_text)
    except (TypeError, ValueError) as error:
        raise UnappliedCashPresentmentQueryError("request_invalid") from error
