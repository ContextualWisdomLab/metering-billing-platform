"""Tenant-scoped payment-intent presentment projected from stored commercial facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``payment_intent`` rows.
3. Project amount, #11 status, and the next operator action.
4. Return the intent.  Do not capture, settle, post, or call AIS.

PCI DSS scope stays reduced by never presenting a card PAN (PCI Security
Standards Council, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import PaymentIntentPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredPaymentIntent


PAYMENT_INTENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_RECORD_RECEIPT = "record_receipt"
PROJECTED_INTENT_STATUS = "projected"


def next_operator_action(payment_intent_status: str) -> str:
    """Return record_receipt or wait from the stored #11 status only.

    A projected intent is ready for a commercial receipt.  Cancelled and
    rejected intents wait.  This path never invents captured or settled.
    """
    if payment_intent_status == PROJECTED_INTENT_STATUS:
        return OPERATOR_ACTION_RECORD_RECEIPT
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class PaymentIntentPresentmentResult:
    """Buyer-facing projection of one stored payment intent."""

    payment_intent_id: UUID
    tenant_reference: str
    collection_case_id: UUID
    currency_code: str
    payment_amount: Decimal
    payment_intent_status: str
    projected_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "payment_intent_presentment_contract_version": (
                PAYMENT_INTENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "payment_intent_id": str(self.payment_intent_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "payment_amount": format_exact_decimal(self.payment_amount),
            "payment_intent_status": self.payment_intent_status,
            "projected_at": _format_projected_at(self.projected_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/payment-intents``."""
        return {
            "payment_intent_id": str(self.payment_intent_id),
            "payment_amount": format_exact_decimal(self.payment_amount),
            "currency_code": self.currency_code,
            "payment_intent_status": self.payment_intent_status,
            "projected_at": _format_projected_at(self.projected_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class PaymentIntentPresentmentPage:
    """One tenant-scoped page of payment-intent summaries."""

    payment_intents: tuple[PaymentIntentPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{payment_intents, next_cursor}`` with summary items."""
        return {
            "payment_intents": [item.as_summary_dict() for item in self.payment_intents],
            "next_cursor": self.next_cursor,
        }


class PaymentIntentPresentmentService:
    """Read-only projector of stored payment intents into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_payment_intent(
        self, tenant_reference: str, payment_intent_id: UUID
    ) -> PaymentIntentPresentmentResult:
        """Return one same-tenant intent, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change intent, receipt, or proposal status.
        """
        tenant = self._require_tenant(tenant_reference)
        payment_intent = self.ledger.get_payment_intent(payment_intent_id)
        if (
            payment_intent is None
            or payment_intent.tenant_account_id != tenant.tenant_account_id
        ):
            raise PaymentIntentPresentmentQueryError("payment_intent_not_found")
        return self._project_intent(tenant.tenant_reference, payment_intent)

    def list_payment_intents(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> PaymentIntentPresentmentPage:
        """Return one tenant page of intent summaries without mutating intents.

        Order is ``projected_at`` then ``payment_intent_id``.  The envelope is
        ``payment_intents`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_payment_intents(tenant.tenant_account_id),
            key=lambda intent: (intent.projected_at, intent.payment_intent_id),
        )
        matched: list[StoredPaymentIntent] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.projected_at, stored.payment_intent_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.projected_at, last.payment_intent_id)
        return PaymentIntentPresentmentPage(
            payment_intents=tuple(
                self._project_intent(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise PaymentIntentPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_intent(
        self, tenant_reference: str, payment_intent: StoredPaymentIntent
    ) -> PaymentIntentPresentmentResult:
        """Project one stored intent plus the next commercial action."""
        return PaymentIntentPresentmentResult(
            payment_intent_id=payment_intent.payment_intent_id,
            tenant_reference=tenant_reference,
            collection_case_id=payment_intent.collection_case_id,
            currency_code=payment_intent.currency_code,
            payment_amount=parse_invoice_amount(payment_intent.payment_amount),
            payment_intent_status=payment_intent.payment_intent_status,
            projected_at=payment_intent.projected_at,
            next_operator_action=next_operator_action(payment_intent.payment_intent_status),
        )


def _format_projected_at(projected_at: datetime) -> str:
    """Render ``projected_at`` as a timezone-aware ISO 8601 instant."""
    return projected_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise PaymentIntentPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise PaymentIntentPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise PaymentIntentPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(projected_at: datetime, payment_intent_id: UUID) -> str:
    """Encode the keyset cursor as projected_at then payment_intent_id."""
    return f"{_format_projected_at(projected_at)}|{payment_intent_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        projected_text, intent_text = cursor.split("|", 1)
        return parse_iso8601_datetime(projected_text), UUID(intent_text)
    except (TypeError, ValueError) as error:
        raise PaymentIntentPresentmentQueryError("request_invalid") from error
