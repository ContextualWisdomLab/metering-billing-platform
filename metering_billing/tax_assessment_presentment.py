"""Tenant-scoped tax-assessment presentment projected from stored tax facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``tax_assessment``.
3. Project amounts, rate, and the next action.
4. Return the assessment.  Do not assess, propose a journal, or invent tax.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import TaxAssessmentPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredTaxAssessment


TAX_ASSESSMENT_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_PROPOSE_JOURNAL = "propose_journal"


def next_operator_action() -> str:
    """Return propose_journal.  Assess the draft, then propose the journal."""
    return OPERATOR_ACTION_PROPOSE_JOURNAL


@dataclass(frozen=True)
class TaxAssessmentPresentmentResult:
    """Buyer-facing projection of one stored tax assessment."""

    tax_assessment_id: UUID
    tenant_reference: str
    invoice_draft_id: UUID
    tax_rate_version_id: UUID
    tax_rate_version: int
    tax_code: str
    tax_rate: Decimal
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    source_payload_hash: str
    assessed_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "tax_assessment_presentment_contract_version": (
                TAX_ASSESSMENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "tax_assessment_id": str(self.tax_assessment_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "tax_rate_version_id": str(self.tax_rate_version_id),
            "tax_rate_version": self.tax_rate_version,
            "tax_code": self.tax_code,
            "tax_rate": format_exact_decimal(self.tax_rate),
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "source_payload_hash": self.source_payload_hash,
            "assessed_at": _format_assessed_at(self.assessed_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/tax-assessments``."""
        return {
            "tax_assessment_id": str(self.tax_assessment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "assessed_at": _format_assessed_at(self.assessed_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class TaxAssessmentPresentmentPage:
    """One tenant-scoped page of tax-assessment summaries."""

    tax_assessments: tuple[TaxAssessmentPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{tax_assessments, next_cursor}`` with summary items."""
        return {
            "tax_assessments": [item.as_summary_dict() for item in self.tax_assessments],
            "next_cursor": self.next_cursor,
        }


class TaxAssessmentPresentmentService:
    """Read-only projector of stored tax assessments into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_tax_assessment(
        self, tenant_reference: str, tax_assessment_id: UUID
    ) -> TaxAssessmentPresentmentResult:
        """Return one same-tenant stored assessment, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not assess, propose a journal, or invent tax policy.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_tax_assessment(tax_assessment_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise TaxAssessmentPresentmentQueryError("tax_assessment_not_found")
        return self._project_assessment(tenant.tenant_reference, stored)

    def list_tax_assessments(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> TaxAssessmentPresentmentPage:
        """Return one tenant page of tax summaries without mutating tax.

        Order is ``assessed_at`` then ``tax_assessment_id``.  The envelope is
        ``tax_assessments`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_tax_assessments(tenant.tenant_account_id),
            key=lambda assessment: (assessment.assessed_at, assessment.tax_assessment_id),
        )
        matched: list[StoredTaxAssessment] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.assessed_at,
                stored.tax_assessment_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.assessed_at, last.tax_assessment_id)
        return TaxAssessmentPresentmentPage(
            tax_assessments=tuple(
                self._project_assessment(tenant.tenant_reference, stored)
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise TaxAssessmentPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_assessment(
        self, tenant_reference: str, stored: StoredTaxAssessment
    ) -> TaxAssessmentPresentmentResult:
        """Project one stored assessment using only persisted commercial fields."""
        return TaxAssessmentPresentmentResult(
            tax_assessment_id=stored.tax_assessment_id,
            tenant_reference=tenant_reference,
            invoice_draft_id=stored.invoice_draft_id,
            tax_rate_version_id=stored.tax_rate_version_id,
            tax_rate_version=stored.tax_rate_version_number,
            tax_code=stored.tax_code,
            tax_rate=stored.tax_rate,
            currency_code=stored.currency_code,
            tax_exclusive_amount=stored.tax_exclusive_amount,
            tax_amount=stored.tax_amount,
            tax_inclusive_amount=stored.tax_inclusive_amount,
            source_payload_hash=stored.source_payload_hash,
            assessed_at=stored.assessed_at,
            next_operator_action=next_operator_action(),
        )


def _format_assessed_at(assessed_at: datetime) -> str:
    """Render an assessment timestamp as a timezone-aware ISO 8601 instant."""
    return assessed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise TaxAssessmentPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise TaxAssessmentPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise TaxAssessmentPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(assessed_at: datetime, tax_assessment_id: UUID) -> str:
    """Encode the keyset cursor as assessed_at then tax_assessment_id."""
    return f"{_format_assessed_at(assessed_at)}|{tax_assessment_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        assessed_text, assessment_text = cursor.split("|", 1)
        return parse_iso8601_datetime(assessed_text), UUID(assessment_text)
    except (TypeError, ValueError) as error:
        raise TaxAssessmentPresentmentQueryError("request_invalid") from error
