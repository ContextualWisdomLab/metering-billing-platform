"""Release one held collection dispute and restore the open case.

The service is the buyer-facing release path:

1. Resolve the tenant and same-tenant held ``collection_dispute``.
2. Flip the existing hold row to ``released``. Do not insert a second hold.
3. Restore case status to ``open``, or ``dunning`` when notices already exist.

Replay of the same tenant and ``collection_dispute_id`` returns the stored
release and never changes remaining outstanding.  A crash after
``mark_collection_dispute_released`` and before
``mark_collection_case_released_from_dispute`` is healed by the next
replay when the stored case is still ``disputed``.  First successful
release enqueues one ``dispute.released`` outbox event.  Replay of that
release does not enqueue a second row.  The path does not emit a journal,
unwind tax, capture payment, call AIS, write off, settle, or void.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_DISPUTED_STATUS,
    COLLECTION_CASE_DUNNING_STATUS,
    COLLECTION_CASE_OPEN_STATUS,
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
from metering_billing.webhook_outbox import (
    EVENT_TYPE_DISPUTE_RELEASED,
    enqueue_accepted_fact,
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

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``dispute.released`` facts for the #24 envelope.

        The payload is a reference plus hash and the exact remaining
        outstanding at release.  Collection-case status, operator action,
        PII, PAN, secrets, statutory identifiers, and dispute-reason
        blobs are omitted.
        """
        if (
            self.collection_dispute_id is None
            or self.collection_case_id is None
            or self.invoice_draft_id is None
        ):
            raise ValueError(
                "rejected collection dispute release has no webhook event data"
            )
        if self.released_at is None:
            raise ValueError("accepted collection dispute releases must include released_at")
        if self.collection_dispute_status != COLLECTION_DISPUTE_RELEASED_STATUS:
            raise ValueError("collection dispute is not released")
        if self.remaining_outstanding_amount is None:
            raise ValueError(
                "accepted collection dispute releases must include remaining outstanding"
            )
        payload: dict[str, object] = {
            "collection_dispute_id": str(self.collection_dispute_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "collection_dispute_release_contract_version": (
                self.collection_dispute_release_contract_version
            ),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_dispute_status": self.collection_dispute_status,
            "released_at": _format_released_at(self.released_at),
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
        Another tenant cannot see or release that hold.  First successful
        release enqueues one ``dispute.released`` outbox event.  Replay of
        that release does not enqueue a second row.
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
            updated_case = _heal_case_after_recorded_release(self.ledger, collection_case)
            result = _from_stored(
                stored,
                updated_case,
                tenant.tenant_reference,
                CollectionDisputeReleaseOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_dispute_released(self.ledger, tenant.tenant_reference, result)
            return result
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
        result = _from_stored(
            released,
            updated_case,
            tenant.tenant_reference,
            CollectionDisputeReleaseOutcomeCode.ACCEPTED,
        )
        _enqueue_dispute_released(self.ledger, tenant.tenant_reference, result)
        return result


def _enqueue_dispute_released(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: CollectionDisputeReleaseResult,
) -> None:
    """Append one ``dispute.released`` outbox row for a stored release.

    Replay of the same tenant, event type, ``collection_dispute_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next release replay.  Remaining
    outstanding in the envelope is the stored release snapshot, not a
    later-mutated case remaining.
    """
    if result.collection_dispute_id is None or result.released_at is None:
        raise ValueError(
            "accepted collection dispute releases must include identity and released_at"
        )
    stored = ledger.get_collection_dispute(result.collection_dispute_id)
    if stored is None:
        raise ValueError(
            "accepted collection dispute releases must include identity and released_at"
        )
    if (
        stored.collection_dispute_status != COLLECTION_DISPUTE_RELEASED_STATUS
        or stored.released_at is None
    ):
        raise ValueError("collection dispute is not released")
    payload = result.as_webhook_event_data()
    remaining = stored.remaining_outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    payload["remaining_outstanding_amount"] = format_exact_decimal(remaining)
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_DISPUTE_RELEASED,
        result.collection_dispute_id,
        payload,
        stored.released_at,
    )


def _heal_case_after_recorded_release(
    ledger: MemoryUsageLedger,
    collection_case: StoredCollectionCase,
) -> StoredCollectionCase:
    """Restore a disputed case left disputed after a recorded release.

    Already-``open`` or ``dunning`` cases stay as-is.  Replay does not
    change remaining.
    """
    if collection_case.collection_case_status in {
        COLLECTION_CASE_OPEN_STATUS,
        COLLECTION_CASE_DUNNING_STATUS,
    }:
        return collection_case
    if collection_case.collection_case_status != COLLECTION_CASE_DISPUTED_STATUS:
        return collection_case
    return ledger.mark_collection_case_released_from_dispute(
        collection_case.collection_case_id
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
