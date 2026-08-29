"""Tenant-scoped issued-invoice presentment from stored commercial snapshots.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``issued_invoice``.
3. Project identity, draft source, frozen totals, optional matching
   tax_assessment_id, and the next action.
4. Return the snapshot.  Do not reissue, collect, credit, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import IssuedInvoicePresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.issued_invoice import OPERATOR_ACTION_COLLECT, _format_signed_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredIssuedInvoice,
    StoredTaxAssessment,
)


ISSUED_INVOICE_PRESENTMENT_CONTRACT_VERSION = 2
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


def next_operator_action() -> str:
    """Return collect so operators use existing collection or credit flows."""
    return OPERATOR_ACTION_COLLECT


@dataclass(frozen=True)
class IssuedInvoicePresentmentLine:
    """One statement line projected from a stored issued-invoice line."""

    line_number: int
    billing_account_reference: str
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal
    line_type: str = "usage"
    late_adjustment_invoice_adjustment_id: UUID | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "line_number": self.line_number,
            "billing_account_reference": self.billing_account_reference,
            "meter_code": self.meter_code,
            "unit_code": self.unit_code,
            "rated_quantity": format_exact_decimal(self.rated_quantity),
            "unit_price_amount": format_exact_decimal(self.unit_price_amount),
            "line_total_amount": _format_signed_decimal(self.line_total_amount),
            "line_type": self.line_type,
        }
        if self.late_adjustment_invoice_adjustment_id is not None:
            payload["late_adjustment_invoice_adjustment_id"] = str(
                self.late_adjustment_invoice_adjustment_id
            )
        return payload


@dataclass(frozen=True)
class IssuedInvoicePresentmentResult:
    """Buyer-facing projection of one stored issued invoice.

    ``tax_assessment_id`` is the stored commercial assessment whose
    exclusive, tax, and inclusive amounts still match this snapshot.
    """

    issued_invoice_id: UUID
    tenant_reference: str
    invoice_draft_id: UUID
    rating_run_id: UUID
    usage_snapshot_hash: str
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    issued_invoice_status: str
    issued_at: datetime
    due_at: datetime | None
    source_payload_hash: str
    issued_invoice_contract_version: int
    next_operator_action: str
    issued_invoice_lines: tuple[IssuedInvoicePresentmentLine, ...]
    tax_assessment_id: UUID | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "issued_invoice_presentment_contract_version": (
                ISSUED_INVOICE_PRESENTMENT_CONTRACT_VERSION
            ),
            "issued_invoice_id": str(self.issued_invoice_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "rating_run_id": str(self.rating_run_id),
            "usage_snapshot_hash": self.usage_snapshot_hash,
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_invoice_status": self.issued_invoice_status,
            "issued_at": _format_issued_at(self.issued_at),
            "source_payload_hash": self.source_payload_hash,
            "issued_invoice_contract_version": self.issued_invoice_contract_version,
            "next_operator_action": self.next_operator_action,
            "issued_invoice_lines": [
                line.as_contract_dict() for line in self.issued_invoice_lines
            ],
        }
        if self.due_at is not None:
            payload["due_at"] = _format_issued_at(self.due_at)
        if self.tax_assessment_id is not None:
            payload["tax_assessment_id"] = str(self.tax_assessment_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "issued_invoice_id": str(self.issued_invoice_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class IssuedInvoicePresentmentPage:
    """One tenant-scoped page of issued-invoice summaries."""

    issued_invoices: tuple[IssuedInvoicePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{issued_invoices, next_cursor}`` with summaries."""
        return {
            "issued_invoices": [item.as_summary_dict() for item in self.issued_invoices],
            "next_cursor": self.next_cursor,
        }


class IssuedInvoicePresentmentService:
    """Read-only projector of stored issued_invoice rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_issued_invoice(
        self, tenant_reference: str, issued_invoice_id: UUID
    ) -> IssuedInvoicePresentmentResult:
        """Return one same-tenant stored snapshot, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not reissue, collect, credit, invent amounts, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_issued_invoice(issued_invoice_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise IssuedInvoicePresentmentQueryError("issued_invoice_not_found")
        return self._project_invoice(tenant.tenant_reference, stored)

    def list_issued_invoices(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> IssuedInvoicePresentmentPage:
        """Return one tenant page of issued-invoice summaries without reissuing.

        Order is ``issued_at`` then ``issued_invoice_id``.
        The envelope is ``issued_invoices`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_issued_invoices_for_tenant(tenant.tenant_account_id),
            key=lambda invoice: (invoice.issued_at, invoice.issued_invoice_id),
        )
        matched: list[StoredIssuedInvoice] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.issued_at,
                stored.issued_invoice_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.issued_at, last.issued_invoice_id)
        return IssuedInvoicePresentmentPage(
            issued_invoices=tuple(
                self._project_invoice(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise IssuedInvoicePresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_invoice(
        self, tenant_reference: str, stored: StoredIssuedInvoice
    ) -> IssuedInvoicePresentmentResult:
        """Project one stored snapshot using only persisted commercial fields."""
        assessment = self.ledger.find_tax_assessment_for_draft(
            stored.tenant_account_id, stored.invoice_draft_id
        )
        return IssuedInvoicePresentmentResult(
            issued_invoice_id=stored.issued_invoice_id,
            tenant_reference=tenant_reference,
            invoice_draft_id=stored.invoice_draft_id,
            rating_run_id=stored.rating_run_id,
            usage_snapshot_hash=stored.usage_snapshot_hash,
            currency_code=stored.currency_code,
            tax_exclusive_amount=stored.tax_exclusive_amount,
            tax_amount=stored.tax_amount,
            tax_inclusive_amount=stored.tax_inclusive_amount,
            issued_invoice_status=stored.issued_invoice_status,
            issued_at=stored.issued_at,
            due_at=stored.due_at,
            source_payload_hash=stored.source_payload_hash,
            issued_invoice_contract_version=ISSUED_INVOICE_PRESENTMENT_CONTRACT_VERSION,
            next_operator_action=next_operator_action(),
            issued_invoice_lines=tuple(
                IssuedInvoicePresentmentLine(
                    line_number=line.line_number,
                    billing_account_reference=line.billing_account_reference,
                    meter_code=line.meter_code,
                    unit_code=line.unit_code,
                    rated_quantity=line.rated_quantity,
                    unit_price_amount=line.unit_price_amount,
                    line_total_amount=line.line_total_amount,
                    line_type=line.line_type,
                    late_adjustment_invoice_adjustment_id=(
                        line.late_adjustment_invoice_adjustment_id
                    ),
                )
                for line in stored.issued_invoice_lines
            ),
            tax_assessment_id=_matching_tax_assessment_id(assessment, stored),
        )


def _matching_tax_assessment_id(
    assessment: StoredTaxAssessment | None, stored: StoredIssuedInvoice
) -> UUID | None:
    """Return the stored assessment id only when frozen issued totals still match.

    A later assessment on the same draft is not the source of the issued
    snapshot.  The read does not copy assessment amounts onto the invoice.
    """
    if assessment is None:
        return None
    issued_totals = (
        stored.tax_exclusive_amount,
        stored.tax_amount,
        stored.tax_inclusive_amount,
    )
    assessed_totals = (
        assessment.tax_exclusive_amount,
        assessment.tax_amount,
        assessment.tax_inclusive_amount,
    )
    if issued_totals != assessed_totals:
        return None
    return assessment.tax_assessment_id


def _format_issued_at(issued_at: datetime) -> str:
    """Render an issue timestamp as a timezone-aware ISO 8601 instant."""
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise IssuedInvoicePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise IssuedInvoicePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise IssuedInvoicePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(issued_at: datetime, issued_invoice_id: UUID) -> str:
    """Encode the keyset cursor as issued_at then issued invoice id."""
    return f"{_format_issued_at(issued_at)}|{issued_invoice_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        issued_text, invoice_text = cursor.split("|", 1)
        return parse_iso8601_datetime(issued_text), UUID(invoice_text)
    except (TypeError, ValueError) as error:
        raise IssuedInvoicePresentmentQueryError("request_invalid") from error
