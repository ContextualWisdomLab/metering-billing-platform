"""Tenant-scoped presentment for immutable late-adjustment facts.

The read path exposes the recorded commercial evidence and its next action. It
does not apply the adjustment, re-rate usage, or create an accounting proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import LateAdjustmentPresentmentQueryError
from metering_billing.period_close import LateAdjustment
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger


LATE_ADJUSTMENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_APPLY = "apply_late_adjustment"
OPERATOR_ACTION_RATE = "rate_late_adjustment"
OPERATOR_ACTION_RECORD_INVOICE_ADJUSTMENT = "record_invoice_adjustment"
OPERATOR_ACTION_ISSUE_INVOICE = "issue_invoice"


def next_operator_action(
    *, applied: bool = False, rated: bool = False, invoice_adjusted: bool = False
) -> str:
    """Return the next action for recorded evidence."""
    if invoice_adjusted:
        return OPERATOR_ACTION_ISSUE_INVOICE
    if rated:
        return OPERATOR_ACTION_RECORD_INVOICE_ADJUSTMENT
    return OPERATOR_ACTION_RATE if applied else OPERATOR_ACTION_APPLY


@dataclass(frozen=True)
class LateAdjustmentPresentmentResult:
    """Buyer-facing projection of one stored late adjustment."""

    late_adjustment_id: UUID
    tenant_reference: str
    source_period_id: UUID
    target_period_id: UUID
    adjustment_kind: str
    adjustment_amount: Decimal
    currency_code: str
    source_reference: str
    source_payload_hash: str
    recorded_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published by the presentment schema."""
        return {
            "late_adjustment_presentment_contract_version": (
                LATE_ADJUSTMENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "late_adjustment_id": str(self.late_adjustment_id),
            "tenant_reference": self.tenant_reference,
            "source_period_id": str(self.source_period_id),
            "target_period_id": str(self.target_period_id),
            "adjustment_kind": self.adjustment_kind,
            "adjustment_amount": format(self.adjustment_amount, "f"),
            "currency_code": self.currency_code,
            "source_reference": self.source_reference,
            "source_payload_hash": self.source_payload_hash,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope for late-adjustment summaries."""
        return {
            "late_adjustment_id": str(self.late_adjustment_id),
            "adjustment_kind": self.adjustment_kind,
            "adjustment_amount": format(self.adjustment_amount, "f"),
            "currency_code": self.currency_code,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class LateAdjustmentPresentmentPage:
    """One tenant-scoped page of late-adjustment summaries."""

    late_adjustments: tuple[LateAdjustmentPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{late_adjustments, next_cursor}`` only."""
        return {
            "late_adjustments": [
                item.as_summary_dict() for item in self.late_adjustments
            ],
            "next_cursor": self.next_cursor,
        }


class LateAdjustmentPresentmentService:
    """Read-only projector of stored late adjustments into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_late_adjustment(
        self, tenant_reference: str, late_adjustment_id: UUID
    ) -> LateAdjustmentPresentmentResult:
        """Return one same-tenant adjustment, or fail closed."""
        tenant = self._require_tenant(tenant_reference)
        if not isinstance(late_adjustment_id, UUID):
            raise LateAdjustmentPresentmentQueryError("late_adjustment_not_found")
        adjustment = self.ledger.get_late_adjustment(
            tenant.tenant_reference, late_adjustment_id
        )
        if adjustment is None:
            raise LateAdjustmentPresentmentQueryError("late_adjustment_not_found")
        return self._project_adjustment(
            tenant.tenant_reference,
            adjustment,
            applied=self.ledger.find_late_adjustment_application(
                tenant.tenant_account_id, adjustment.late_adjustment_id
            )
            is not None,
            rated=self.ledger.find_late_adjustment_rating(
                tenant.tenant_account_id, adjustment.late_adjustment_id
            )
            is not None,
            invoice_adjusted=self._invoice_adjusted(
                tenant.tenant_account_id, adjustment.late_adjustment_id
            ),
        )

    def list_late_adjustments(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> LateAdjustmentPresentmentPage:
        """Return tenant-scoped late-adjustment summaries in recorded order."""
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_late_adjustments(tenant.tenant_reference),
            key=lambda adjustment: (
                adjustment.recorded_at,
                adjustment.late_adjustment_id,
            ),
        )
        matched = tuple(
            adjustment
            for adjustment in stored_rows
            if cursor_key is None
            or (adjustment.recorded_at, adjustment.late_adjustment_id) > cursor_key
        )
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.recorded_at, last.late_adjustment_id)
        return LateAdjustmentPresentmentPage(
            late_adjustments=tuple(
                self._project_adjustment(
                    tenant.tenant_reference,
                    adjustment,
                    applied=self.ledger.find_late_adjustment_application(
                        tenant.tenant_account_id, adjustment.late_adjustment_id
                    )
                    is not None,
                    rated=self.ledger.find_late_adjustment_rating(
                        tenant.tenant_account_id, adjustment.late_adjustment_id
                    )
                    is not None,
                    invoice_adjusted=self._invoice_adjusted(
                        tenant.tenant_account_id, adjustment.late_adjustment_id
                    ),
                )
                for adjustment in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise LateAdjustmentPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _invoice_adjusted(
        self, tenant_account_id: UUID, late_adjustment_id: UUID
    ) -> bool:
        """Return whether the rated adjustment is attached to an invoice draft."""
        rating = self.ledger.find_late_adjustment_rating(
            tenant_account_id, late_adjustment_id
        )
        return rating is not None and self.ledger.find_late_adjustment_invoice_adjustment(
            tenant_account_id, rating.late_adjustment_rating_id
        ) is not None

    @staticmethod
    def _project_adjustment(
        tenant_reference: str,
        adjustment: LateAdjustment,
        *,
        applied: bool = False,
        rated: bool = False,
        invoice_adjusted: bool = False,
    ) -> LateAdjustmentPresentmentResult:
        """Project only persisted commercial evidence."""
        return LateAdjustmentPresentmentResult(
            late_adjustment_id=adjustment.late_adjustment_id,
            tenant_reference=tenant_reference,
            source_period_id=adjustment.source_period_id,
            target_period_id=adjustment.target_period_id,
            adjustment_kind=adjustment.adjustment_kind.value,
            adjustment_amount=adjustment.adjustment_amount,
            currency_code=adjustment.currency_code,
            source_reference=adjustment.source_reference,
            source_payload_hash=adjustment.source_payload_hash,
            recorded_at=adjustment.recorded_at,
            next_operator_action=next_operator_action(
                applied=applied, rated=rated, invoice_adjusted=invoice_adjusted
            ),
        )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Render a timezone-aware instant as UTC ISO 8601."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to one through the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise LateAdjustmentPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise LateAdjustmentPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise LateAdjustmentPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(recorded_at: datetime, late_adjustment_id: UUID) -> str:
    """Encode the deterministic recorded-at/identifier keyset cursor."""
    return f"{_format_recorded_at(recorded_at)}|{late_adjustment_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        recorded_text, adjustment_text = cursor.split("|", 1)
        return parse_iso8601_datetime(recorded_text), UUID(adjustment_text)
    except (TypeError, ValueError) as error:
        raise LateAdjustmentPresentmentQueryError("request_invalid") from error
