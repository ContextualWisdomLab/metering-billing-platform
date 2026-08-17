"""Tenant-scoped invoice-draft presentment projected from stored commercial facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``invoice_draft``.
3. Project tax, accepted credits, amount due, optional collection, and lines.
4. Return a statement.  Do not post, collect, credit, or call AIS.

IFRS 15 treats the statement as presentation of consideration, not proof that
revenue has been earned (IFRS Foundation, 2024).  ISO 20022 keeps a commercial
invoice document separate from a posted financial message (International
Organization for Standardization, 2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import InvoicePresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredInvoiceDraft


INVOICE_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
ZERO = Decimal("0")


def remaining_amount_due(inclusive_amount: Decimal, credited_amount: Decimal) -> Decimal:
    """Return inclusive consideration minus credits, never below zero.

    Both inputs must already be exact non-negative decimals.  The clamp keeps a
    corrupt over-credit from presenting a negative amount due.
    """
    inclusive = parse_invoice_amount(inclusive_amount)
    credited = parse_invoice_amount(credited_amount)
    remaining = inclusive - credited
    if remaining < ZERO:
        return ZERO
    return remaining


@dataclass(frozen=True)
class InvoicePresentmentLine:
    """One statement line projected from a stored invoice-draft line."""

    line_number: int
    metric_code: str
    quantity: Decimal
    unit_amount: Decimal
    line_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "line_number": self.line_number,
            "metric_code": self.metric_code,
            "quantity": format_exact_decimal(self.quantity),
            "unit_amount": format_exact_decimal(self.unit_amount),
            "line_amount": format_exact_decimal(self.line_amount),
        }


@dataclass(frozen=True)
class InvoicePresentmentResult:
    """Buyer-facing statement for one stored invoice draft."""

    invoice_draft_id: UUID
    tenant_reference: str
    currency_code: str
    drafted_at: datetime
    rating_run_id: UUID
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    credited_amount: Decimal
    amount_due: Decimal
    invoice_lines: tuple[InvoicePresentmentLine, ...]
    collection_case_id: UUID | None = None
    collection_outstanding: Decimal | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "invoice_presentment_contract_version": INVOICE_PRESENTMENT_CONTRACT_VERSION,
            "invoice_draft_id": str(self.invoice_draft_id),
            "tenant_reference": self.tenant_reference,
            "currency_code": self.currency_code,
            "drafted_at": _format_drafted_at(self.drafted_at),
            "rating_run_id": str(self.rating_run_id),
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "credited_amount": format_exact_decimal(self.credited_amount),
            "amount_due": format_exact_decimal(self.amount_due),
            "invoice_lines": [line.as_contract_dict() for line in self.invoice_lines],
        }
        if self.collection_case_id is not None:
            payload["collection_case_id"] = str(self.collection_case_id)
        if self.collection_outstanding is not None:
            payload["collection_outstanding"] = format_exact_decimal(
                self.collection_outstanding
            )
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/invoice-drafts``."""
        return {
            "invoice_draft_id": str(self.invoice_draft_id),
            "amount_due": format_exact_decimal(self.amount_due),
            "currency_code": self.currency_code,
            "drafted_at": _format_drafted_at(self.drafted_at),
        }


@dataclass(frozen=True)
class InvoicePresentmentPage:
    """One tenant-scoped page of invoice-draft statement summaries."""

    invoice_drafts: tuple[InvoicePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{invoice_drafts, next_cursor}`` with summary items."""
        return {
            "invoice_drafts": [item.as_summary_dict() for item in self.invoice_drafts],
            "next_cursor": self.next_cursor,
        }


class InvoicePresentmentService:
    """Read-only projector of stored invoice drafts into commercial statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_invoice_draft(
        self, tenant_reference: str, invoice_draft_id: UUID
    ) -> InvoicePresentmentResult:
        """Return one same-tenant statement, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change draft, tax, credit, collection, or proposal status.
        """
        tenant = self._require_tenant(tenant_reference)
        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            raise InvoicePresentmentQueryError("invoice_draft_not_found")
        return self._project_statement(tenant.tenant_reference, invoice_draft)

    def list_invoice_drafts(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> InvoicePresentmentPage:
        """Return one tenant page of statement summaries without mutating drafts.

        Order is ``drafted_at`` then ``invoice_draft_id``.  Items carry id,
        amount due, currency, and drafted time only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_invoice_drafts(tenant.tenant_account_id),
            key=lambda draft: (draft.recorded_at, draft.invoice_draft_id),
        )
        matched: list[StoredInvoiceDraft] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.recorded_at, stored.invoice_draft_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.recorded_at, last.invoice_draft_id)
        return InvoicePresentmentPage(
            invoice_drafts=tuple(
                self._project_statement(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise InvoicePresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_statement(
        self, tenant_reference: str, invoice_draft: StoredInvoiceDraft
    ) -> InvoicePresentmentResult:
        """Project one stored draft plus tax, credits, and collection."""
        exclusive, tax_amount, inclusive = self._tax_amounts(invoice_draft)
        credited_amount = self._credited_amount(
            invoice_draft.tenant_account_id, invoice_draft.invoice_draft_id
        )
        collection_case = self.ledger.find_collection_case(
            invoice_draft.tenant_account_id, invoice_draft.invoice_draft_id
        )
        collection_case_id = None
        collection_outstanding = None
        if collection_case is not None:
            collection_case_id = collection_case.collection_case_id
            collection_outstanding = parse_invoice_amount(collection_case.outstanding_amount)
        return InvoicePresentmentResult(
            invoice_draft_id=invoice_draft.invoice_draft_id,
            tenant_reference=tenant_reference,
            currency_code=invoice_draft.currency_code,
            drafted_at=invoice_draft.recorded_at,
            rating_run_id=invoice_draft.rating_run_id,
            tax_exclusive_amount=exclusive,
            tax_amount=tax_amount,
            tax_inclusive_amount=inclusive,
            credited_amount=credited_amount,
            amount_due=remaining_amount_due(inclusive, credited_amount),
            invoice_lines=tuple(
                InvoicePresentmentLine(
                    line_number=line.line_number,
                    metric_code=line.meter_code,
                    quantity=parse_invoice_amount(line.rated_quantity),
                    unit_amount=parse_invoice_amount(line.unit_price_amount),
                    line_amount=parse_invoice_amount(line.line_total_amount),
                )
                for line in invoice_draft.invoice_draft_lines
            ),
            collection_case_id=collection_case_id,
            collection_outstanding=collection_outstanding,
        )

    def _tax_amounts(
        self, invoice_draft: StoredInvoiceDraft
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return exclusive, tax, and inclusive amounts, zeroing tax when unassessed."""
        assessment = self.ledger.find_tax_assessment_for_draft(
            invoice_draft.tenant_account_id, invoice_draft.invoice_draft_id
        )
        if assessment is None:
            exclusive = parse_invoice_amount(invoice_draft.drafted_total_amount)
            return exclusive, ZERO, exclusive
        exclusive = parse_invoice_amount(assessment.tax_exclusive_amount)
        tax_amount = parse_invoice_amount(assessment.tax_amount)
        inclusive = parse_invoice_amount(assessment.tax_inclusive_amount)
        if exclusive + tax_amount != inclusive:
            raise InvoicePresentmentQueryError("request_invalid")
        return exclusive, tax_amount, inclusive

    def _credited_amount(self, tenant_account_id: UUID, invoice_draft_id: UUID) -> Decimal:
        """Sum accepted credits for one tenant draft as an exact decimal."""
        credited = ZERO
        for credit in self.ledger.list_credit_adjustments(tenant_account_id):
            if credit.invoice_draft_id != invoice_draft_id:
                continue
            credited += parse_invoice_amount(credit.credit_amount)
        return credited


def _format_drafted_at(drafted_at: datetime) -> str:
    """Render ``drafted_at`` as a timezone-aware ISO 8601 instant."""
    return drafted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise InvoicePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise InvoicePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise InvoicePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(drafted_at: datetime, invoice_draft_id: UUID) -> str:
    """Encode the keyset cursor as drafted_at then invoice_draft_id."""
    return f"{_format_drafted_at(drafted_at)}|{invoice_draft_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        drafted_text, draft_text = cursor.split("|", 1)
        return parse_iso8601_datetime(drafted_text), UUID(draft_text)
    except (TypeError, ValueError) as error:
        raise InvoicePresentmentQueryError("request_invalid") from error
