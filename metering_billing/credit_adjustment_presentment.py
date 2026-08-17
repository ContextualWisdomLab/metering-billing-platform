"""Tenant-scoped credit-adjustment presentment projected from stored facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``credit_adjustment``.
3. Project credit amount, stored tax split, and the next action.
4. Return the credit.  Do not post, call AIS, or invent a journal.

IFRS 15 treats a commercial credit as consideration evidence, not a posted
reversal (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent
read (Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import CreditAdjustmentPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCreditAdjustment


CREDIT_ADJUSTMENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"


def next_operator_action() -> str:
    """Return wait.  The credit is recorded; AIS pulls the validated journal."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class CreditAdjustmentPresentmentResult:
    """Buyer-facing projection of one stored credit adjustment."""

    credit_adjustment_id: UUID
    tenant_reference: str
    invoice_draft_id: UUID
    currency_code: str
    credit_amount: Decimal
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    credit_adjustment_status: str
    recorded_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "credit_adjustment_presentment_contract_version": (
                CREDIT_ADJUSTMENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "credit_amount": format_exact_decimal(self.credit_amount),
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "credit_adjustment_status": self.credit_adjustment_status,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/credit-adjustments``."""
        return {
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "credit_amount": format_exact_decimal(self.credit_amount),
            "currency_code": self.currency_code,
            "credit_adjustment_status": self.credit_adjustment_status,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CreditAdjustmentPresentmentPage:
    """One tenant-scoped page of credit-adjustment summaries."""

    credit_adjustments: tuple[CreditAdjustmentPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{credit_adjustments, next_cursor}`` with summary items."""
        return {
            "credit_adjustments": [item.as_summary_dict() for item in self.credit_adjustments],
            "next_cursor": self.next_cursor,
        }


class CreditAdjustmentPresentmentService:
    """Read-only projector of stored credit adjustments into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_credit_adjustment(
        self, tenant_reference: str, credit_adjustment_id: UUID
    ) -> CreditAdjustmentPresentmentResult:
        """Return one same-tenant credit, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change credit or proposal status.
        """
        tenant = self._require_tenant(tenant_reference)
        credit = self.ledger.get_credit_adjustment(credit_adjustment_id)
        if credit is None or credit.tenant_account_id != tenant.tenant_account_id:
            raise CreditAdjustmentPresentmentQueryError("credit_adjustment_not_found")
        return self._project_credit(tenant.tenant_reference, credit)

    def list_credit_adjustments(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CreditAdjustmentPresentmentPage:
        """Return one tenant page of credit summaries without mutating credits.

        Order is ``recorded_at`` then ``credit_adjustment_id``.  The envelope is
        ``credit_adjustments`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_credit_adjustments(tenant.tenant_account_id),
            key=lambda credit: (credit.recorded_at, credit.credit_adjustment_id),
        )
        matched: list[StoredCreditAdjustment] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.recorded_at, stored.credit_adjustment_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.recorded_at, last.credit_adjustment_id)
        return CreditAdjustmentPresentmentPage(
            credit_adjustments=tuple(
                self._project_credit(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CreditAdjustmentPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_credit(
        self, tenant_reference: str, credit: StoredCreditAdjustment
    ) -> CreditAdjustmentPresentmentResult:
        """Project one stored credit using only persisted commercial fields."""
        return CreditAdjustmentPresentmentResult(
            credit_adjustment_id=credit.credit_adjustment_id,
            tenant_reference=tenant_reference,
            invoice_draft_id=credit.invoice_draft_id,
            currency_code=credit.currency_code,
            credit_amount=parse_invoice_amount(credit.credit_amount),
            tax_exclusive_amount=parse_invoice_amount(credit.tax_exclusive_amount),
            tax_amount=parse_invoice_amount(credit.tax_amount),
            credit_adjustment_status="recorded",
            recorded_at=credit.recorded_at,
            next_operator_action=next_operator_action(),
        )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Render ``recorded_at`` as a timezone-aware ISO 8601 instant."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CreditAdjustmentPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CreditAdjustmentPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CreditAdjustmentPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(recorded_at: datetime, credit_adjustment_id: UUID) -> str:
    """Encode the keyset cursor as recorded_at then credit_adjustment_id."""
    return f"{_format_recorded_at(recorded_at)}|{credit_adjustment_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        recorded_text, credit_text = cursor.split("|", 1)
        return parse_iso8601_datetime(recorded_text), UUID(credit_text)
    except (TypeError, ValueError) as error:
        raise CreditAdjustmentPresentmentQueryError("request_invalid") from error
