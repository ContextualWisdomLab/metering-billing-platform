"""Release one held collection dispute and restore the open case.

The service is the buyer-facing release path:

1. Resolve the tenant and same-tenant held ``collection_dispute``.
2. Flip the existing hold row to ``released``. Do not insert a second hold.
3. Restore case status to ``open``, or ``dunning`` when notices already exist.

Replay of the same tenant and ``collection_dispute_id`` returns the stored
release and never changes remaining outstanding.  The path does not emit a
journal, webhook, unwind tax, capture payment, call AIS, write off, settle,
or void.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_SETTLED_STATUS,
    COLLECTION_CASE_VOIDED_STATUS,
)
from metering_billing.collection_dispute import COLLECTION_DISPUTE_STATUS
from metering_billing.errors import (
    CollectionDisputeReleaseOutcomeCode,
    CollectionDisputeReleaseRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionDispute,
)


Clock = Callable[[], datetime]
COLLECTION_DISPUTE_RELEASE_CONTRACT_VERSION = 1
COLLECTION_DISPUTE_RELEASED_STATUS = "released"
OPERATOR_ACTION_WAIT = "wait"


@dataclass(frozen=True)
class CollectionDisputeReleaseResult:
    """Buyer-facing result of releasing one held collection dispute."""

    collection_dispute_release_outcome_code: CollectionDisputeReleaseOutcomeCode
    collection_dispute_release_contract_version: int
    collection_dispute_id: UUID | None
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    remaining_outstanding_amount: Decimal | None
    collection_dispute_status: str | None
    collection_case_status: str | None
    released_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: CollectionDisputeReleaseRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published release, or a sparse rejected result."""
        outcome = self.collection_dispute_release_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, CollectionDisputeReleaseOutcomeCode)
            else str(outcome)
        )
        if outcome_text == CollectionDisputeReleaseOutcomeCode.REJECTED:
            return {
                "collection_dispute_release_contract_version": (
                    self.collection_dispute_release_contract_version
                ),
                "collection_dispute_release_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CollectionDisputeReleaseOutcomeCode.ACCEPTED
            and outcome_text != CollectionDisputeReleaseOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(
                f"unsupported collection dispute release outcome: {outcome_text}"
            )
        payload: dict[str, object] = {
            "collection_dispute_release_contract_version": (
                self.collection_dispute_release_contract_version
            ),
            "collection_dispute_release_outcome_code": outcome_text,
            "collection_dispute_id": str(self.collection_dispute_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_dispute_status": self.collection_dispute_status,
            "collection_case_status": self.collection_case_status,
            "released_at": _format_released_at(self.released_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class CollectionDisputeReleaseService:
    """In-place writer that releases one held commercial collection dispute."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def release_collection_dispute(
        self,
        tenant_reference: str,
        collection_dispute_id: UUID,
        currency_code: str | None = None,
    ) -> CollectionDisputeReleaseResult:
        """Release one same-tenant held collection dispute.

        Replay of the same tenant and ``collection_dispute_id`` returns the
        stored release and does not change remaining outstanding again.
        Another tenant cannot see or release that hold.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionDisputeReleaseRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        stored = self.ledger.get_collection_dispute(collection_dispute_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            return _rejected(
                CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_FOUND
            )
        collection_case = self.ledger.get_collection_case(stored.collection_case_id)
        if collection_case is None:
            return _rejected(
                CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
            )
        if stored.collection_dispute_status == COLLECTION_DISPUTE_RELEASED_STATUS:
            return _from_stored(
                stored,
                collection_case,
                tenant.tenant_reference,
                CollectionDisputeReleaseOutcomeCode.DUPLICATE_REPLAY,
            )
        if stored.collection_dispute_status != COLLECTION_DISPUTE_STATUS:
            return _rejected(
                CollectionDisputeReleaseRejectionReasonCode.COLLECTION_DISPUTE_NOT_HELD
            )
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(
                CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_SETTLED
            )
        if collection_case.collection_case_status == COLLECTION_CASE_VOIDED_STATUS:
            return _rejected(
                CollectionDisputeReleaseRejectionReasonCode.COLLECTION_CASE_VOIDED
            )
        if currency_code is not None and currency_code != stored.currency_code:
            return _rejected(CollectionDisputeReleaseRejectionReasonCode.CURRENCY_MISMATCH)
        released_at = self._clock()
        released = self.ledger.mark_collection_dispute_released(
            stored.collection_dispute_id, released_at
        )
        updated_case = self.ledger.mark_collection_case_released_from_dispute(
            stored.collection_case_id
        )
        return _from_stored(
            released,
            updated_case,
            tenant.tenant_reference,
            CollectionDisputeReleaseOutcomeCode.ACCEPTED,
        )


def _rejected(
    reason_code: CollectionDisputeReleaseRejectionReasonCode,
) -> CollectionDisputeReleaseResult:
    """Build a rejected result without writing a release or changing outstanding."""
    return CollectionDisputeReleaseResult(
        collection_dispute_release_outcome_code=CollectionDisputeReleaseOutcomeCode.REJECTED,
        collection_dispute_release_contract_version=COLLECTION_DISPUTE_RELEASE_CONTRACT_VERSION,
        collection_dispute_id=None,
        collection_case_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        remaining_outstanding_amount=None,
        collection_dispute_status=None,
        collection_case_status=None,
        released_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredCollectionDispute,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: CollectionDisputeReleaseOutcomeCode,
) -> CollectionDisputeReleaseResult:
    """Project a persisted release and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return CollectionDisputeReleaseResult(
        collection_dispute_release_outcome_code=outcome,
        collection_dispute_release_contract_version=COLLECTION_DISPUTE_RELEASE_CONTRACT_VERSION,
        collection_dispute_id=stored.collection_dispute_id,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        remaining_outstanding_amount=remaining,
        collection_dispute_status=stored.collection_dispute_status,
        collection_case_status=collection_case.collection_case_status,
        released_at=stored.released_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_released_at(released_at: datetime | None) -> str:
    """Render ``released_at`` as a timezone-aware ISO 8601 instant."""
    if released_at is None:
        raise ValueError("accepted collection dispute releases must include released_at")
    return released_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
