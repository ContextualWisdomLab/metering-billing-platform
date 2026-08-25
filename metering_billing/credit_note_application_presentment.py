"""Tenant-scoped credit-note-application presentment from stored apply rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``credit_note_application``.
3. Project identity, applied amount, and current remaining outstanding.
4. Return the statement.  Do not re-apply, capture payment, or call AIS.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.credit_note_application import (
    OPERATOR_ACTION_COLLECT,
    OPERATOR_ACTION_WAIT,
)
from metering_billing.errors import CreditNoteApplicationPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCreditNoteApplication


CREDIT_NOTE_APPLICATION_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class CreditNoteApplicationPresentmentResult:
    """Buyer-facing projection of one stored credit-note application."""

    credit_note_application_id: UUID
    tenant_reference: str
    issued_credit_note_id: UUID
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    applied_amount: Decimal
    remaining_outstanding_amount: Decimal
    credit_note_application_status: str
    collection_case_status: str
    applied_at: datetime
    source_payload_hash: str
    issued_credit_note_source_payload_hash: str
    issued_credit_note_contract_version: int
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "credit_note_application_presentment_contract_version": (
                CREDIT_NOTE_APPLICATION_PRESENTMENT_CONTRACT_VERSION
            ),
            "credit_note_application_id": str(self.credit_note_application_id),
            "tenant_reference": self.tenant_reference,
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "applied_amount": format_exact_decimal(self.applied_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "credit_note_application_status": self.credit_note_application_status,
            "collection_case_status": self.collection_case_status,
            "applied_at": _format_applied_at(self.applied_at),
            "source_payload_hash": self.source_payload_hash,
            "issued_credit_note_source_payload_hash": (
                self.issued_credit_note_source_payload_hash
            ),
            "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "credit_note_application_id": str(self.credit_note_application_id),
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "collection_case_id": str(self.collection_case_id),
            "applied_amount": format_exact_decimal(self.applied_amount),
            "applied_at": _format_applied_at(self.applied_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class CreditNoteApplicationPresentmentPage:
    """One tenant-scoped page of credit-note-application summaries."""

    credit_note_applications: tuple[CreditNoteApplicationPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{credit_note_applications, next_cursor}`` with summaries."""
        return {
            "credit_note_applications": [
                item.as_summary_dict() for item in self.credit_note_applications
            ],
            "next_cursor": self.next_cursor,
        }


class CreditNoteApplicationPresentmentService:
    """Read-only projector of stored credit_note_application rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_credit_note_application(
        self, tenant_reference: str, credit_note_application_id: UUID
    ) -> CreditNoteApplicationPresentmentResult:
        """Return one same-tenant stored application, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not re-apply, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_credit_note_application(credit_note_application_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise CreditNoteApplicationPresentmentQueryError("credit_note_application_not_found")
        return self._project_application(tenant.tenant_reference, stored)

    def list_credit_note_applications(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> CreditNoteApplicationPresentmentPage:
        """Return one tenant page of application summaries without re-applying.

        Order is ``applied_at`` then ``credit_note_application_id``.
        The envelope is ``credit_note_applications`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_credit_note_applications_for_tenant(tenant.tenant_account_id),
            key=lambda application: (application.applied_at, application.credit_note_application_id),
        )
        matched: list[StoredCreditNoteApplication] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.applied_at,
                stored.credit_note_application_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.applied_at, last.credit_note_application_id)
        return CreditNoteApplicationPresentmentPage(
            credit_note_applications=tuple(
                self._project_application(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CreditNoteApplicationPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_application(
        self, tenant_reference: str, stored: StoredCreditNoteApplication
    ) -> CreditNoteApplicationPresentmentResult:
        """Project one stored application using the current collection balance."""
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None:
            raise CreditNoteApplicationPresentmentQueryError("credit_note_application_not_found")
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        return CreditNoteApplicationPresentmentResult(
            credit_note_application_id=stored.credit_note_application_id,
            tenant_reference=tenant_reference,
            issued_credit_note_id=stored.issued_credit_note_id,
            collection_case_id=stored.collection_case_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            applied_amount=stored.applied_amount,
            remaining_outstanding_amount=remaining,
            credit_note_application_status=stored.credit_note_application_status,
            collection_case_status=collection_case.collection_case_status,
            applied_at=stored.applied_at,
            source_payload_hash=stored.source_payload_hash,
            issued_credit_note_source_payload_hash=stored.issued_credit_note_source_payload_hash,
            issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
            next_operator_action=OPERATOR_ACTION_WAIT if remaining == 0 else OPERATOR_ACTION_COLLECT,
        )


def _format_applied_at(applied_at: datetime) -> str:
    """Render an apply timestamp as a timezone-aware ISO 8601 instant."""
    return applied_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise CreditNoteApplicationPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise CreditNoteApplicationPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise CreditNoteApplicationPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(applied_at: datetime, credit_note_application_id: UUID) -> str:
    """Encode the keyset cursor as applied_at then application id."""
    return f"{_format_applied_at(applied_at)}|{credit_note_application_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        applied_text, application_text = cursor.split("|", 1)
        return parse_iso8601_datetime(applied_text), UUID(application_text)
    except (TypeError, ValueError) as error:
        raise CreditNoteApplicationPresentmentQueryError("request_invalid") from error
