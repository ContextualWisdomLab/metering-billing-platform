"""Hold one open collection case as a commercial dispute.

The service is the buyer-facing hold path:

1. Resolve the tenant and same-tenant collection case.
2. Persist one append-only ``collection_dispute`` per case.
3. Flip case status to ``disputed`` without changing remaining outstanding.

Replay of the same tenant and ``collection_case_id`` returns the stored
hold and never re-flips remaining outstanding.  A crash after insert and
before ``mark_collection_case_disputed`` is healed by the next replay
when the stored hold's case is still ``open`` or ``dunning``.  First
successful hold enqueues one ``dispute.held`` outbox event.  Replay of
that hold does not enqueue a second row.  The path does not emit a
journal, unwind tax, capture payment, call AIS, write off, settle, or
void.  Release is the sibling command on the same hold row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_DISPUTED_STATUS,
    COLLECTION_CASE_DUNNING_STATUS,
    COLLECTION_CASE_OPEN_STATUS,
    COLLECTION_CASE_SETTLED_STATUS,
    COLLECTION_CASE_VOIDED_STATUS,
)
from metering_billing.errors import (
    CollectionDisputeOutcomeCode,
    CollectionDisputeRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionDispute,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_DISPUTE_HELD,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
COLLECTION_DISPUTE_CONTRACT_VERSION = 1
COLLECTION_DISPUTE_STATUS = "held"
OPERATOR_ACTION_WAIT = "wait"


def compute_dispute_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical dispute-hold identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CollectionDisputeResult:
    """Buyer-facing result of holding one collection case as disputed."""

    collection_dispute_outcome_code: CollectionDisputeOutcomeCode
    collection_dispute_contract_version: int
    collection_dispute_id: UUID | None
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    remaining_outstanding_amount: Decimal | None
    collection_dispute_status: str | None
    collection_case_status: str | None
    held_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: CollectionDisputeRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published hold, or a sparse rejected result."""
        outcome = self.collection_dispute_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, CollectionDisputeOutcomeCode)
            else str(outcome)
        )
        if outcome_text == CollectionDisputeOutcomeCode.REJECTED:
            return {
                "collection_dispute_contract_version": (
                    self.collection_dispute_contract_version
                ),
                "collection_dispute_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CollectionDisputeOutcomeCode.ACCEPTED
            and outcome_text != CollectionDisputeOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported collection dispute outcome: {outcome_text}")
        payload: dict[str, object] = {
            "collection_dispute_contract_version": self.collection_dispute_contract_version,
            "collection_dispute_outcome_code": outcome_text,
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
            "held_at": _format_held_at(self.held_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``dispute.held`` facts for the #24 envelope.

        The payload is a reference plus hash and the exact remaining
        outstanding at hold.  Collection-case status, operator action,
        PII, PAN, secrets, statutory identifiers, and dispute-reason
        blobs are omitted.
        """
        if (
            self.collection_dispute_id is None
            or self.collection_case_id is None
            or self.invoice_draft_id is None
        ):
            raise ValueError("rejected collection dispute has no webhook event data")
        if self.held_at is None:
            raise ValueError("accepted collection disputes must include held_at")
        if self.remaining_outstanding_amount is None:
            raise ValueError("accepted collection disputes must include remaining outstanding")
        payload: dict[str, object] = {
            "collection_dispute_id": str(self.collection_dispute_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "collection_dispute_contract_version": (
                self.collection_dispute_contract_version
            ),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_dispute_status": self.collection_dispute_status,
            "held_at": _format_held_at(self.held_at),
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class CollectionDisputeService:
    """Append-only writer of a commercial collection-case dispute hold."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def hold_collection_case(
        self,
        tenant_reference: str,
        collection_case_id: UUID,
        currency_code: str | None = None,
    ) -> CollectionDisputeResult:
        """Hold one same-tenant open or dunning collection case as disputed.

        Replay of the same tenant and ``collection_case_id`` returns the
        stored ``collection_dispute_id`` and does not change remaining
        outstanding again.  Another tenant cannot see or hold that case.
        New dunning fails closed while the hold exists.  First successful
        hold enqueues one ``dispute.held`` outbox event.  Replay of that
        hold does not enqueue a second row.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionDisputeRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_collection_dispute(
            tenant.tenant_account_id, collection_case_id
        )
        if existing is not None:
            if existing.collection_dispute_status != COLLECTION_DISPUTE_STATUS:
                return _rejected(
                    CollectionDisputeRejectionReasonCode.COLLECTION_DISPUTE_RELEASED
                )
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(
                    CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
                )
            current_case = _heal_case_after_recorded_hold(self.ledger, current_case)
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                CollectionDisputeOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_dispute_held(self.ledger, tenant.tenant_reference, result)
            return result
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_SETTLED)
        if collection_case.collection_case_status == COLLECTION_CASE_VOIDED_STATUS:
            return _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_VOIDED)
        if collection_case.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
            return _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_DISPUTED)
        if collection_case.collection_case_status not in {
            COLLECTION_CASE_OPEN_STATUS,
            COLLECTION_CASE_DUNNING_STATUS,
        }:
            return _rejected(CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)
        if currency_code is not None and currency_code != collection_case.currency_code:
            return _rejected(CollectionDisputeRejectionReasonCode.CURRENCY_MISMATCH)
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        issued_invoice = self.ledger.find_issued_invoice(
            collection_case.tenant_account_id, collection_case.invoice_draft_id
        )
        issued_invoice_id = (
            issued_invoice.issued_invoice_id if issued_invoice is not None else None
        )
        source_payload_hash = compute_dispute_payload_hash(
            _canonical_dispute_snapshot(collection_case, issued_invoice_id, remaining)
        )
        candidate = StoredCollectionDispute(
            collection_dispute_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            collection_case_id=collection_case.collection_case_id,
            invoice_draft_id=collection_case.invoice_draft_id,
            issued_invoice_id=issued_invoice_id,
            collection_dispute_contract_version=COLLECTION_DISPUTE_CONTRACT_VERSION,
            source_payload_hash=source_payload_hash,
            currency_code=collection_case.currency_code,
            remaining_outstanding_amount=remaining,
            collection_dispute_status=COLLECTION_DISPUTE_STATUS,
            held_at=self._clock(),
        )
        stored = self.ledger.insert_collection_dispute(candidate)
        if stored.collection_dispute_id != candidate.collection_dispute_id:
            current_case = self.ledger.get_collection_case(stored.collection_case_id)
            if current_case is None:
                return _rejected(
                    CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
                )
            if stored.collection_dispute_status != COLLECTION_DISPUTE_STATUS:
                return _rejected(
                    CollectionDisputeRejectionReasonCode.COLLECTION_DISPUTE_RELEASED
                )
            current_case = _heal_case_after_recorded_hold(self.ledger, current_case)
            result = _from_stored(
                stored,
                current_case,
                tenant.tenant_reference,
                CollectionDisputeOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_dispute_held(self.ledger, tenant.tenant_reference, result)
            return result
        updated_case = self.ledger.mark_collection_case_disputed(
            collection_case.collection_case_id
        )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            CollectionDisputeOutcomeCode.ACCEPTED,
        )
        _enqueue_dispute_held(self.ledger, tenant.tenant_reference, result)
        return result


def _heal_case_after_recorded_hold(
    ledger: MemoryUsageLedger,
    collection_case: StoredCollectionCase,
) -> StoredCollectionCase:
    """Flip an unused open or dunning case left open after a recorded hold.

    Already-``disputed`` cases stay as-is.  Replay does not change remaining.
    """
    if collection_case.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
        return collection_case
    if collection_case.collection_case_status not in {
        COLLECTION_CASE_OPEN_STATUS,
        COLLECTION_CASE_DUNNING_STATUS,
    }:
        return collection_case
    return ledger.mark_collection_case_disputed(collection_case.collection_case_id)


def _enqueue_dispute_held(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: CollectionDisputeResult,
) -> None:
    """Append one ``dispute.held`` outbox row for a stored hold.

    Replay of the same tenant, event type, ``collection_dispute_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next hold replay.  Remaining
    outstanding in the envelope is the stored hold snapshot, not a
    later-mutated case remaining.
    """
    if result.collection_dispute_id is None or result.held_at is None:
        raise ValueError(
            "accepted collection disputes must include identity and held_at"
        )
    stored = ledger.get_collection_dispute(result.collection_dispute_id)
    if stored is None:
        raise ValueError(
            "accepted collection disputes must include identity and held_at"
        )
    payload = result.as_webhook_event_data()
    remaining = stored.remaining_outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    payload["remaining_outstanding_amount"] = format_exact_decimal(remaining)
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_DISPUTE_HELD,
        result.collection_dispute_id,
        payload,
        stored.held_at,
    )


def _canonical_dispute_snapshot(
    collection_case: StoredCollectionCase,
    issued_invoice_id: UUID | None,
    remaining_outstanding_amount: Decimal,
) -> dict[str, object]:
    """Return case, optional invoice, currency, remaining snapshot, and version."""
    payload: dict[str, object] = {
        "collection_case_id": str(collection_case.collection_case_id),
        "invoice_draft_id": str(collection_case.invoice_draft_id),
        "currency_code": collection_case.currency_code,
        "remaining_outstanding_amount": format_exact_decimal(remaining_outstanding_amount),
        "collection_dispute_contract_version": COLLECTION_DISPUTE_CONTRACT_VERSION,
    }
    if issued_invoice_id is not None:
        payload["issued_invoice_id"] = str(issued_invoice_id)
    return payload


def _rejected(
    reason_code: CollectionDisputeRejectionReasonCode,
) -> CollectionDisputeResult:
    """Build a rejected result without writing a hold or changing outstanding."""
    return CollectionDisputeResult(
        collection_dispute_outcome_code=CollectionDisputeOutcomeCode.REJECTED,
        collection_dispute_contract_version=COLLECTION_DISPUTE_CONTRACT_VERSION,
        collection_dispute_id=None,
        collection_case_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        remaining_outstanding_amount=None,
        collection_dispute_status=None,
        collection_case_status=None,
        held_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredCollectionDispute,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: CollectionDisputeOutcomeCode,
) -> CollectionDisputeResult:
    """Project a persisted hold and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return CollectionDisputeResult(
        collection_dispute_outcome_code=outcome,
        collection_dispute_contract_version=stored.collection_dispute_contract_version,
        collection_dispute_id=stored.collection_dispute_id,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        remaining_outstanding_amount=remaining,
        collection_dispute_status=stored.collection_dispute_status,
        collection_case_status=collection_case.collection_case_status,
        held_at=stored.held_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_held_at(held_at: datetime | None) -> str:
    """Render ``held_at`` as a timezone-aware ISO 8601 instant."""
    if held_at is None:
        raise ValueError("accepted collection disputes must include held_at")
    return held_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
