"""Tenant-scoped payment-receipt presentment projected from stored commercial facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``payment_receipt`` and current collection case.
3. Project received amount, remaining outstanding, and the next action.
4. Return the receipt.  Do not capture, post, or call AIS.

PCI DSS scope stays reduced by never presenting a card PAN (PCI Security
Standards Council, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.collection_case import _derived_collection_case_status
from metering_billing.errors import PaymentReceiptPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredPaymentReceipt


PAYMENT_RECEIPT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
ZERO = Decimal("0")
OPERATOR_ACTION_RECORD_RECEIPT = "record_receipt"
OPERATOR_ACTION_DRAIN_OR_WAIT = "drain_or_wait"


def next_operator_action(remaining_outstanding_amount: Decimal) -> str:
    """Return record_receipt or drain_or_wait from remaining outstanding only.

    Residual outstanding still accepts another commercial receipt.  Zero
    remaining waits for the existing #13 cash journal to be pulled or drained.
    """
    if remaining_outstanding_amount > ZERO:
        return OPERATOR_ACTION_RECORD_RECEIPT
    return OPERATOR_ACTION_DRAIN_OR_WAIT


@dataclass(frozen=True)
class PaymentReceiptPresentmentResult:
    """Buyer-facing projection of one stored payment receipt."""

    payment_receipt_id: UUID
    tenant_reference: str
    payment_intent_id: UUID
    collection_case_id: UUID
    currency_code: str
    received_amount: Decimal
    remaining_outstanding_amount: Decimal
    payment_receipt_status: str
    collection_case_status: str
    received_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "payment_receipt_presentment_contract_version": (
                PAYMENT_RECEIPT_PRESENTMENT_CONTRACT_VERSION
            ),
            "payment_receipt_id": str(self.payment_receipt_id),
            "tenant_reference": self.tenant_reference,
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "received_amount": format_exact_decimal(self.received_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "payment_receipt_status": self.payment_receipt_status,
            "collection_case_status": self.collection_case_status,
            "received_at": _format_received_at(self.received_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/payment-receipts``."""
        return {
            "payment_receipt_id": str(self.payment_receipt_id),
            "received_amount": format_exact_decimal(self.received_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "currency_code": self.currency_code,
            "payment_receipt_status": self.payment_receipt_status,
            "received_at": _format_received_at(self.received_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class PaymentReceiptPresentmentPage:
    """One tenant-scoped page of payment-receipt summaries."""

    payment_receipts: tuple[PaymentReceiptPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{payment_receipts, next_cursor}`` with summary items."""
        return {
            "payment_receipts": [item.as_summary_dict() for item in self.payment_receipts],
            "next_cursor": self.next_cursor,
        }


class PaymentReceiptPresentmentService:
    """Read-only projector of stored payment receipts into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_payment_receipt(
        self, tenant_reference: str, payment_receipt_id: UUID
    ) -> PaymentReceiptPresentmentResult:
        """Return one same-tenant receipt, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change receipt, case, or proposal status.
        """
        tenant = self._require_tenant(tenant_reference)
        payment_receipt = self.ledger.get_payment_receipt(payment_receipt_id)
        if (
            payment_receipt is None
            or payment_receipt.tenant_account_id != tenant.tenant_account_id
        ):
            raise PaymentReceiptPresentmentQueryError("payment_receipt_not_found")
        return self._project_receipt(tenant.tenant_reference, payment_receipt)

    def list_payment_receipts(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> PaymentReceiptPresentmentPage:
        """Return one tenant page of receipt summaries without mutating receipts.

        Order is ``received_at`` then ``payment_receipt_id``.  The envelope is
        ``payment_receipts`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_payment_receipts(tenant.tenant_account_id),
            key=lambda receipt: (receipt.received_at, receipt.payment_receipt_id),
        )
        matched: list[StoredPaymentReceipt] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.received_at, stored.payment_receipt_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.received_at, last.payment_receipt_id)
        return PaymentReceiptPresentmentPage(
            payment_receipts=tuple(
                self._project_receipt(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise PaymentReceiptPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_receipt(
        self, tenant_reference: str, payment_receipt: StoredPaymentReceipt
    ) -> PaymentReceiptPresentmentResult:
        """Project one stored receipt plus current collection outstanding."""
        collection_case = self.ledger.get_collection_case(payment_receipt.collection_case_id)
        if collection_case is None:
            raise PaymentReceiptPresentmentQueryError("payment_receipt_not_found")
        remaining = parse_invoice_amount(collection_case.outstanding_amount)
        dunning_events = self.ledger.list_collection_dunning_events(
            collection_case.collection_case_id
        )
        return PaymentReceiptPresentmentResult(
            payment_receipt_id=payment_receipt.payment_receipt_id,
            tenant_reference=tenant_reference,
            payment_intent_id=payment_receipt.payment_intent_id,
            collection_case_id=payment_receipt.collection_case_id,
            currency_code=payment_receipt.currency_code,
            received_amount=parse_invoice_amount(payment_receipt.received_amount),
            remaining_outstanding_amount=remaining,
            payment_receipt_status=payment_receipt.payment_receipt_status,
            collection_case_status=_derived_collection_case_status(
                collection_case, dunning_events
            ),
            received_at=payment_receipt.received_at,
            next_operator_action=next_operator_action(remaining),
        )


def _format_received_at(received_at: datetime) -> str:
    """Render ``received_at`` as a timezone-aware ISO 8601 instant."""
    return received_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise PaymentReceiptPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise PaymentReceiptPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise PaymentReceiptPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(received_at: datetime, payment_receipt_id: UUID) -> str:
    """Encode the keyset cursor as received_at then payment_receipt_id."""
    return f"{_format_received_at(received_at)}|{payment_receipt_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        received_text, receipt_text = cursor.split("|", 1)
        return parse_iso8601_datetime(received_text), UUID(receipt_text)
    except (TypeError, ValueError) as error:
        raise PaymentReceiptPresentmentQueryError("request_invalid") from error
