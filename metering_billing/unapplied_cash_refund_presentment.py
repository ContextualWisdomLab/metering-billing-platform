"""Tenant-scoped leftover-refund presentment from stored refund rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``unapplied_cash_refund``.
3. Project identity, refund amount, and current leftover status.
4. Return the statement.  Do not refund again, capture cards, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import (
    UnappliedCashRefundPresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.unapplied_cash_refund import OPERATOR_ACTION_WAIT
from metering_billing.usage_ledger import MemoryUsageLedger, StoredUnappliedCashRefund


UNAPPLIED_CASH_REFUND_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class UnappliedCashRefundPresentmentResult:
    """Buyer-facing projection of one stored leftover refund."""

    unapplied_cash_refund_id: UUID
    tenant_reference: str
    unapplied_cash_id: UUID
    payment_receipt_id: UUID
    payment_intent_id: UUID
    collection_case_id: UUID
    currency_code: str
    refund_amount: Decimal
    unapplied_amount: Decimal
    unapplied_cash_refund_status: str
    unapplied_cash_status: str
    refunded_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "unapplied_cash_refund_presentment_contract_version": (
                UNAPPLIED_CASH_REFUND_PRESENTMENT_CONTRACT_VERSION
            ),
            "unapplied_cash_refund_id": str(self.unapplied_cash_refund_id),
            "tenant_reference": self.tenant_reference,
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "refund_amount": format_exact_decimal(self.refund_amount),
            "unapplied_amount": format_exact_decimal(self.unapplied_amount),
            "unapplied_cash_refund_status": self.unapplied_cash_refund_status,
            "unapplied_cash_status": self.unapplied_cash_status,
            "refunded_at": _format_refunded_at(self.refunded_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "unapplied_cash_refund_id": str(self.unapplied_cash_refund_id),
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "refund_amount": format_exact_decimal(self.refund_amount),
            "refunded_at": _format_refunded_at(self.refunded_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class UnappliedCashRefundPresentmentPage:
    """One tenant-scoped page of leftover-refund summaries."""

    unapplied_cash_refunds: tuple[UnappliedCashRefundPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{unapplied_cash_refunds, next_cursor}`` with summaries."""
        return {
            "unapplied_cash_refunds": [
                item.as_summary_dict() for item in self.unapplied_cash_refunds
            ],
            "next_cursor": self.next_cursor,
        }


class UnappliedCashRefundPresentmentService:
    """Read-only projector of stored unapplied_cash_refund rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_unapplied_cash_refund(
        self, tenant_reference: str, unapplied_cash_refund_id: UUID
    ) -> UnappliedCashRefundPresentmentResult:
        """Return one same-tenant stored refund, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The
        read does not refund again, capture cards, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_unapplied_cash_refund(unapplied_cash_refund_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise UnappliedCashRefundPresentmentQueryError("unapplied_cash_refund_not_found")
        return self._project_refund(tenant.tenant_reference, stored)

    def list_unapplied_cash_refunds(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> UnappliedCashRefundPresentmentPage:
        """Return one tenant page of refund summaries without refunding again.

        Order is ``refunded_at`` then ``unapplied_cash_refund_id``.
        The envelope is ``unapplied_cash_refunds`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_unapplied_cash_refunds_for_tenant(tenant.tenant_account_id),
            key=lambda refund: (refund.refunded_at, refund.unapplied_cash_refund_id),
        )
        matched: list[StoredUnappliedCashRefund] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.refunded_at,
                stored.unapplied_cash_refund_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.refunded_at, last.unapplied_cash_refund_id)
        return UnappliedCashRefundPresentmentPage(
            unapplied_cash_refunds=tuple(
                self._project_refund(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise UnappliedCashRefundPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_refund(
        self, tenant_reference: str, stored: StoredUnappliedCashRefund
    ) -> UnappliedCashRefundPresentmentResult:
        """Project one stored refund using the current leftover status."""
        leftover = self.ledger.get_unapplied_cash(stored.unapplied_cash_id)
        if leftover is None:
            raise UnappliedCashRefundPresentmentQueryError("unapplied_cash_refund_not_found")
        return UnappliedCashRefundPresentmentResult(
            unapplied_cash_refund_id=stored.unapplied_cash_refund_id,
            tenant_reference=tenant_reference,
            unapplied_cash_id=stored.unapplied_cash_id,
            payment_receipt_id=stored.payment_receipt_id,
            payment_intent_id=stored.payment_intent_id,
            collection_case_id=stored.collection_case_id,
            currency_code=stored.currency_code,
            refund_amount=stored.refund_amount,
            unapplied_amount=stored.unapplied_amount,
            unapplied_cash_refund_status=stored.unapplied_cash_refund_status,
            unapplied_cash_status=leftover.unapplied_cash_status,
            refunded_at=stored.refunded_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_refunded_at(refunded_at: datetime) -> str:
    """Render a refund timestamp as a timezone-aware ISO 8601 instant."""
    return refunded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise UnappliedCashRefundPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise UnappliedCashRefundPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise UnappliedCashRefundPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(refunded_at: datetime, unapplied_cash_refund_id: UUID) -> str:
    """Encode the keyset cursor as refunded_at then refund id."""
    return f"{_format_refunded_at(refunded_at)}|{unapplied_cash_refund_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        refunded_text, refund_text = cursor.split("|", 1)
        return parse_iso8601_datetime(refunded_text), UUID(refund_text)
    except (TypeError, ValueError) as error:
        raise UnappliedCashRefundPresentmentQueryError("request_invalid") from error
