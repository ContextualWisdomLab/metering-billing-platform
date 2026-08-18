"""Commercial collection cases produced from already-stored invoice drafts.

The service is the buyer-facing collections path:

1. Resolve the tenant.
2. Load that tenant's stored ``invoice_draft``.
3. Open an append-only case whose outstanding equals the tax-inclusive
   amount when a tax assessment exists, otherwise the exact draft total.
4. Append commercial dunning reminders without capturing payment.

The case is a commercial collection record, not a statutory invoice and not a
posted journal (IFRS Foundation, 2024).  A later dispute hold may pause
dunning without changing remaining outstanding.  Payment intents and provider
adapters remain a later increment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from metering_billing.errors import (
    CollectionCaseOutcomeCode,
    CollectionCaseRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionDunningEvent,
    generate_record_id,
)


Clock = Callable[[], datetime]
COLLECTION_CASE_CONTRACT_VERSION = 1
COLLECTION_CASE_OPEN_STATUS = "open"
COLLECTION_CASE_DUNNING_STATUS = "dunning"
COLLECTION_CASE_SETTLED_STATUS = "settled"
COLLECTION_CASE_VOIDED_STATUS = "voided"
COLLECTION_CASE_DISPUTED_STATUS = "disputed"
DUNNING_NOTICE_CODES = frozenset({"first_notice", "overdue_notice"})


def parse_collection_amount(value: Any) -> Decimal:
    """Parse a collection outstanding amount as an exact non-negative decimal.

    Binary floating-point values are rejected at this boundary so a case
    cannot smuggle IEEE inexact money into commercial outstanding.
    """
    if isinstance(value, Decimal):
        return parse_exact_decimal(format_exact_decimal(value))
    return parse_exact_decimal(value)


@dataclass(frozen=True)
class CollectionDunningEventResult:
    """One commercial reminder appended to a collection case."""

    dunning_event_id: UUID
    dunning_event_number: int
    dunning_notice_code: str
    occurred_at: datetime

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the collection-case schema."""
        return {
            "dunning_event_id": str(self.dunning_event_id),
            "dunning_event_number": self.dunning_event_number,
            "dunning_notice_code": self.dunning_notice_code,
            "occurred_at": _format_occurred_at(self.occurred_at),
        }


@dataclass(frozen=True)
class CollectionCaseResult:
    """Buyer-facing result of opening a case or recording a dunning notice."""

    collection_case_outcome_code: CollectionCaseOutcomeCode
    collection_case_contract_version: int
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    collection_case_status: str | None
    outstanding_amount: Decimal | None
    rejection_reason_code: CollectionCaseRejectionReasonCode | None
    dunning_events: tuple[CollectionDunningEventResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published collection case, or a sparse rejected result."""
        outcome = self.collection_case_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, CollectionCaseOutcomeCode) else str(outcome)
        )
        if outcome_text == CollectionCaseOutcomeCode.REJECTED:
            return {
                "collection_case_contract_version": self.collection_case_contract_version,
                "collection_case_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else "invoice_draft_not_found"
                ),
            }
        if (
            outcome_text != CollectionCaseOutcomeCode.ACCEPTED
            and outcome_text != CollectionCaseOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported collection case outcome: {outcome_text}")
        return {
            "collection_case_contract_version": self.collection_case_contract_version,
            "collection_case_outcome_code": outcome_text,
            "collection_case_id": str(self.collection_case_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "collection_case_status": self.collection_case_status,
            "outstanding_amount": format_exact_decimal(self.outstanding_amount),
            "dunning_events": [event.as_contract_dict() for event in self.dunning_events],
        }


class CollectionCaseService:
    """Append-only collection-case opener backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def open_collection_case(
        self, tenant_reference: str, invoice_draft_id: UUID
    ) -> CollectionCaseResult:
        """Open a commercial collection case for one tenant invoice draft.

        A replay of the same tenant and draft returns the stored
        ``collection_case_id`` and exact outstanding.  Another tenant cannot
        see or collect that case.  The operator next sends a dunning notice.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionCaseRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(CollectionCaseRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)

        assessment = self.ledger.find_tax_assessment_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )
        collectible = (
            assessment.tax_inclusive_amount
            if assessment is not None
            else invoice_draft.drafted_total_amount
        )
        outstanding_amount = parse_collection_amount(collectible)
        if outstanding_amount <= 0:
            return _rejected(CollectionCaseRejectionReasonCode.OUTSTANDING_AMOUNT_INVALID)

        existing = self.ledger.find_collection_case(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                CollectionCaseOutcomeCode.DUPLICATE_REPLAY,
                self.ledger.list_collection_dunning_events(existing.collection_case_id),
            )

        stored = self.ledger.insert_collection_case(
            StoredCollectionCase(
                collection_case_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=invoice_draft.invoice_draft_id,
                currency_code=invoice_draft.currency_code,
                collection_case_status=COLLECTION_CASE_OPEN_STATUS,
                outstanding_amount=outstanding_amount,
                opened_at=self._clock(),
            )
        )
        return _from_stored(
            stored, tenant.tenant_reference, CollectionCaseOutcomeCode.ACCEPTED, ()
        )

    def record_dunning_event(
        self,
        tenant_reference: str,
        collection_case_id: UUID,
        dunning_notice_code: str,
    ) -> CollectionCaseResult:
        """Append one commercial reminder to a stored collection case.

        The same tenant, case, and notice code replay the stored event.  The
        reminder does not capture money, change outstanding, or post to AIS.
        """
        if dunning_notice_code not in DUNNING_NOTICE_CODES:
            return _rejected(CollectionCaseRejectionReasonCode.DUNNING_NOTICE_INVALID)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionCaseRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        collection_case = self.ledger.get_collection_case(collection_case_id)
        if collection_case is None or collection_case.tenant_account_id != tenant.tenant_account_id:
            return _rejected(CollectionCaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)

        existing_event = self.ledger.find_collection_dunning_event(
            collection_case.collection_case_id, dunning_notice_code
        )
        if existing_event is not None:
            return _from_stored(
                collection_case,
                tenant.tenant_reference,
                CollectionCaseOutcomeCode.DUPLICATE_REPLAY,
                self.ledger.list_collection_dunning_events(collection_case.collection_case_id),
            )
        if collection_case.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
            return _rejected(CollectionCaseRejectionReasonCode.COLLECTION_CASE_DISPUTED)

        existing_events = self.ledger.list_collection_dunning_events(
            collection_case.collection_case_id
        )
        self.ledger.insert_collection_dunning_event(
            StoredCollectionDunningEvent(
                collection_dunning_event_id=generate_record_id(),
                collection_case_id=collection_case.collection_case_id,
                tenant_account_id=collection_case.tenant_account_id,
                dunning_event_number=len(existing_events) + 1,
                dunning_notice_code=dunning_notice_code,
                occurred_at=self._clock(),
            )
        )
        return _from_stored(
            collection_case,
            tenant.tenant_reference,
            CollectionCaseOutcomeCode.ACCEPTED,
            self.ledger.list_collection_dunning_events(collection_case.collection_case_id),
        )


def _rejected(reason_code: CollectionCaseRejectionReasonCode) -> CollectionCaseResult:
    """Build a rejected result without writing a case or notice."""
    return CollectionCaseResult(
        collection_case_outcome_code=CollectionCaseOutcomeCode.REJECTED,
        collection_case_contract_version=COLLECTION_CASE_CONTRACT_VERSION,
        collection_case_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        currency_code=None,
        collection_case_status=None,
        outstanding_amount=None,
        rejection_reason_code=reason_code,
        dunning_events=(),
    )


def _from_stored(
    stored: StoredCollectionCase,
    tenant_reference: str,
    outcome: CollectionCaseOutcomeCode,
    dunning_events: tuple[StoredCollectionDunningEvent, ...],
) -> CollectionCaseResult:
    """Project a persisted case and its reminders into the buyer-facing result."""
    return CollectionCaseResult(
        collection_case_outcome_code=outcome,
        collection_case_contract_version=COLLECTION_CASE_CONTRACT_VERSION,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        collection_case_status=_derived_collection_case_status(stored, dunning_events),
        outstanding_amount=stored.outstanding_amount,
        rejection_reason_code=None,
        dunning_events=tuple(
            CollectionDunningEventResult(
                dunning_event_id=event.collection_dunning_event_id,
                dunning_event_number=event.dunning_event_number,
                dunning_notice_code=event.dunning_notice_code,
                occurred_at=event.occurred_at,
            )
            for event in dunning_events
        ),
    )


def _derived_collection_case_status(
    stored: StoredCollectionCase,
    dunning_events: tuple[StoredCollectionDunningEvent, ...],
) -> str:
    """Prefer closed or held status over dunning so a paused case does not reopen."""
    if stored.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
        return COLLECTION_CASE_SETTLED_STATUS
    if stored.collection_case_status == COLLECTION_CASE_VOIDED_STATUS:
        return COLLECTION_CASE_VOIDED_STATUS
    if stored.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
        return COLLECTION_CASE_DISPUTED_STATUS
    if dunning_events:
        return COLLECTION_CASE_DUNNING_STATUS
    return stored.collection_case_status


def _format_occurred_at(occurred_at: datetime) -> str:
    """Render ``occurred_at`` as a timezone-aware ISO 8601 instant."""
    return occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
