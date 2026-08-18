"""Tenant-scoped issued-credit-note-void presentment from stored void rows.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``issued_credit_note_void``.
3. Project identity and the frozen voided amount.
4. Return the statement.  Do not re-void, apply, capture payment, or call AIS.

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
    IssuedCreditNoteVoidPresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.issued_credit_note_void import OPERATOR_ACTION_WAIT
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredIssuedCreditNoteVoid


ISSUED_CREDIT_NOTE_VOID_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


@dataclass(frozen=True)
class IssuedCreditNoteVoidPresentmentResult:
    """Buyer-facing projection of one stored issued-credit-note void."""

    issued_credit_note_void_id: UUID
    tenant_reference: str
    issued_credit_note_id: UUID
    credit_adjustment_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    currency_code: str
    voided_amount: Decimal
    issued_credit_note_void_status: str
    voided_at: datetime
    source_payload_hash: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "issued_credit_note_void_presentment_contract_version": (
                ISSUED_CREDIT_NOTE_VOID_PRESENTMENT_CONTRACT_VERSION
            ),
            "issued_credit_note_void_id": str(self.issued_credit_note_void_id),
            "tenant_reference": self.tenant_reference,
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "voided_amount": format_exact_decimal(self.voided_amount),
            "issued_credit_note_void_status": self.issued_credit_note_void_status,
            "voided_at": _format_voided_at(self.voided_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "issued_credit_note_void_id": str(self.issued_credit_note_void_id),
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "voided_amount": format_exact_decimal(self.voided_amount),
            "voided_at": _format_voided_at(self.voided_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class IssuedCreditNoteVoidPresentmentPage:
    """One tenant-scoped page of issued-credit-note-void summaries."""

    issued_credit_note_voids: tuple[IssuedCreditNoteVoidPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{issued_credit_note_voids, next_cursor}`` with summaries."""
        return {
            "issued_credit_note_voids": [
                item.as_summary_dict() for item in self.issued_credit_note_voids
            ],
            "next_cursor": self.next_cursor,
        }


class IssuedCreditNoteVoidPresentmentService:
    """Read-only projector of stored issued_credit_note_void rows."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_issued_credit_note_void(
        self, tenant_reference: str, issued_credit_note_void_id: UUID
    ) -> IssuedCreditNoteVoidPresentmentResult:
        """Return one same-tenant stored void, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not re-void, apply, capture payment, or call AIS.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_issued_credit_note_void(issued_credit_note_void_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise IssuedCreditNoteVoidPresentmentQueryError(
                "issued_credit_note_void_not_found"
            )
        return self._project_void(tenant.tenant_reference, stored)

    def list_issued_credit_note_voids(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> IssuedCreditNoteVoidPresentmentPage:
        """Return one tenant page of void summaries without re-voiding.

        Order is ``voided_at`` then ``issued_credit_note_void_id``.
        The envelope is ``issued_credit_note_voids`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_issued_credit_note_voids_for_tenant(tenant.tenant_account_id),
            key=lambda void_row: (
                void_row.voided_at,
                void_row.issued_credit_note_void_id,
            ),
        )
        matched: list[StoredIssuedCreditNoteVoid] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.voided_at,
                stored.issued_credit_note_void_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(
                last.voided_at, last.issued_credit_note_void_id
            )
        return IssuedCreditNoteVoidPresentmentPage(
            issued_credit_note_voids=tuple(
                self._project_void(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise IssuedCreditNoteVoidPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_void(
        self, tenant_reference: str, stored: StoredIssuedCreditNoteVoid
    ) -> IssuedCreditNoteVoidPresentmentResult:
        """Project one stored void using only persisted commercial fields."""
        return IssuedCreditNoteVoidPresentmentResult(
            issued_credit_note_void_id=stored.issued_credit_note_void_id,
            tenant_reference=tenant_reference,
            issued_credit_note_id=stored.issued_credit_note_id,
            credit_adjustment_id=stored.credit_adjustment_id,
            invoice_draft_id=stored.invoice_draft_id,
            issued_invoice_id=stored.issued_invoice_id,
            currency_code=stored.currency_code,
            voided_amount=stored.voided_amount,
            issued_credit_note_void_status=stored.issued_credit_note_void_status,
            voided_at=stored.voided_at,
            source_payload_hash=stored.source_payload_hash,
            next_operator_action=OPERATOR_ACTION_WAIT,
        )


def _format_voided_at(voided_at: datetime) -> str:
    """Render a void timestamp as a timezone-aware ISO 8601 instant."""
    return voided_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise IssuedCreditNoteVoidPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise IssuedCreditNoteVoidPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise IssuedCreditNoteVoidPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(voided_at: datetime, issued_credit_note_void_id: UUID) -> str:
    """Encode the keyset cursor as voided_at then void id."""
    return f"{_format_voided_at(voided_at)}|{issued_credit_note_void_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        voided_text, void_text = cursor.split("|", 1)
        return parse_iso8601_datetime(voided_text), UUID(void_text)
    except (TypeError, ValueError) as error:
        raise IssuedCreditNoteVoidPresentmentQueryError("request_invalid") from error
