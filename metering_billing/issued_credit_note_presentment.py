"""Tenant-scoped issued-credit-note presentment from stored commercial snapshots.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``issued_credit_note``.
3. Project identity, credit source, frozen totals, optional matching
   tax_assessment_id, and the next action.
4. Return the snapshot.  Do not reissue, capture payment, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.credit_adjustment import CreditSplitError, split_inclusive_credit
from metering_billing.errors import IssuedCreditNotePresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.issued_credit_note import OPERATOR_ACTION_WAIT
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredIssuedCreditNote,
    StoredTaxAssessment,
)


ISSUED_CREDIT_NOTE_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


def next_operator_action() -> str:
    """Return wait so operators leave the validated journal for AIS."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class IssuedCreditNotePresentmentResult:
    """Buyer-facing projection of one stored issued credit note.

    ``tax_assessment_id`` is the stored commercial assessment whose
    current split still reproduces this snapshot's exclusive and tax
    amounts.
    """

    issued_credit_note_id: UUID
    tenant_reference: str
    credit_adjustment_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    issued_credit_note_status: str
    issued_at: datetime
    source_payload_hash: str
    credit_adjustment_source_payload_hash: str
    credit_adjustment_contract_version: int
    credit_reason_code: str
    issued_credit_note_contract_version: int
    next_operator_action: str
    tax_assessment_id: UUID | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "issued_credit_note_presentment_contract_version": (
                ISSUED_CREDIT_NOTE_PRESENTMENT_CONTRACT_VERSION
            ),
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "tenant_reference": self.tenant_reference,
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_credit_note_status": self.issued_credit_note_status,
            "issued_at": _format_issued_at(self.issued_at),
            "source_payload_hash": self.source_payload_hash,
            "credit_adjustment_source_payload_hash": self.credit_adjustment_source_payload_hash,
            "credit_adjustment_contract_version": self.credit_adjustment_contract_version,
            "credit_reason_code": self.credit_reason_code,
            "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        if self.tax_assessment_id is not None:
            payload["tax_assessment_id"] = str(self.tax_assessment_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "currency_code": self.currency_code,
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class IssuedCreditNotePresentmentPage:
    """One tenant-scoped page of issued-credit-note summaries."""

    issued_credit_notes: tuple[IssuedCreditNotePresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{issued_credit_notes, next_cursor}`` with summaries."""
        return {
            "issued_credit_notes": [item.as_summary_dict() for item in self.issued_credit_notes],
            "next_cursor": self.next_cursor,
        }


class IssuedCreditNotePresentmentService:
    """Read-only projector of stored issued_credit_note rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_issued_credit_note(
        self, tenant_reference: str, issued_credit_note_id: UUID
    ) -> IssuedCreditNotePresentmentResult:
        """Return one same-tenant stored snapshot, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not reissue, capture payment, invent amounts, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_issued_credit_note(issued_credit_note_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise IssuedCreditNotePresentmentQueryError("issued_credit_note_not_found")
        return self._project_note(tenant.tenant_reference, stored)

    def list_issued_credit_notes(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> IssuedCreditNotePresentmentPage:
        """Return one tenant page of issued-credit-note summaries without reissuing.

        Order is ``issued_at`` then ``issued_credit_note_id``.
        The envelope is ``issued_credit_notes`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_issued_credit_notes_for_tenant(tenant.tenant_account_id),
            key=lambda note: (note.issued_at, note.issued_credit_note_id),
        )
        matched: list[StoredIssuedCreditNote] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.issued_at,
                stored.issued_credit_note_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.issued_at, last.issued_credit_note_id)
        return IssuedCreditNotePresentmentPage(
            issued_credit_notes=tuple(
                self._project_note(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise IssuedCreditNotePresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_note(
        self, tenant_reference: str, stored: StoredIssuedCreditNote
    ) -> IssuedCreditNotePresentmentResult:
        """Project one stored snapshot using only persisted commercial fields."""
        assessment = self.ledger.find_tax_assessment_for_draft(
            stored.tenant_account_id, stored.invoice_draft_id
        )
        return IssuedCreditNotePresentmentResult(
            issued_credit_note_id=stored.issued_credit_note_id,
            tenant_reference=tenant_reference,
            credit_adjustment_id=stored.credit_adjustment_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            tax_exclusive_amount=stored.tax_exclusive_amount,
            tax_amount=stored.tax_amount,
            tax_inclusive_amount=stored.tax_inclusive_amount,
            issued_credit_note_status=stored.issued_credit_note_status,
            issued_at=stored.issued_at,
            source_payload_hash=stored.source_payload_hash,
            credit_adjustment_source_payload_hash=stored.credit_adjustment_source_payload_hash,
            credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
            credit_reason_code=stored.credit_reason_code,
            issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
            next_operator_action=next_operator_action(),
            tax_assessment_id=_matching_tax_assessment_id(assessment, stored),
        )


def _matching_tax_assessment_id(
    assessment: StoredTaxAssessment | None, stored: StoredIssuedCreditNote
) -> UUID | None:
    """Return the stored assessment id only when it still splits this credit.

    Credit notes freeze a proportional exclusive/tax split, not the full
    assessment totals.  A later assessment that would produce a different
    split is not the source of the snapshot.  The read does not copy
    assessment amounts onto the credit note.
    """
    if assessment is None:
        return None
    try:
        expected_exclusive, expected_tax = split_inclusive_credit(
            stored.tax_inclusive_amount,
            assessment.tax_amount,
            assessment.tax_inclusive_amount,
            stored.currency_code,
        )
    except CreditSplitError:
        return None
    if (expected_exclusive, expected_tax) != (
        stored.tax_exclusive_amount,
        stored.tax_amount,
    ):
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
        raise IssuedCreditNotePresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise IssuedCreditNotePresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise IssuedCreditNotePresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(issued_at: datetime, issued_credit_note_id: UUID) -> str:
    """Encode the keyset cursor as issued_at then issued credit note id."""
    return f"{_format_issued_at(issued_at)}|{issued_credit_note_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        issued_text, note_text = cursor.split("|", 1)
        return parse_iso8601_datetime(issued_text), UUID(note_text)
    except (TypeError, ValueError) as error:
        raise IssuedCreditNotePresentmentQueryError("request_invalid") from error
