"""Write off leftover remaining on one open collection case.

The service is the buyer-facing write-off path:

1. Resolve the tenant and same-tenant collection case.
2. Require remaining outstanding to be strictly positive.
3. Persist one append-only ``collection_write_off`` per case.
4. Zero remaining outstanding without flipping the case to ``settled``.

Replay of the same tenant and ``collection_case_id`` returns the stored
write-off and never re-zeros outstanding.  First successful write-off
enqueues one existing ``write_off.recorded`` outbox event; replay of
the same write-off does not enqueue a second row.  The path does not
emit a journal, unwind tax, capture payment, call AIS, or settle the
case.  #46 remains the explicit settle-when-zero command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.collection_case import COLLECTION_CASE_SETTLED_STATUS
from metering_billing.errors import (
    CollectionWriteOffOutcomeCode,
    CollectionWriteOffRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionWriteOff,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_WRITE_OFF_RECORDED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
COLLECTION_WRITE_OFF_CONTRACT_VERSION = 1
COLLECTION_WRITE_OFF_STATUS = "recorded"
OPERATOR_ACTION_SETTLE = "settle"
OPERATOR_ACTION_WAIT = "wait"


def compute_write_off_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical write-off identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CollectionWriteOffResult:
    """Buyer-facing result of writing off leftover collection remaining."""

    collection_write_off_outcome_code: CollectionWriteOffOutcomeCode
    collection_write_off_contract_version: int
    collection_write_off_id: UUID | None
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    write_off_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    collection_write_off_status: str | None
    collection_case_status: str | None
    written_off_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: CollectionWriteOffRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published write-off, or a sparse rejected result."""
        outcome = self.collection_write_off_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, CollectionWriteOffOutcomeCode)
            else str(outcome)
        )
        if outcome_text == CollectionWriteOffOutcomeCode.REJECTED:
            return {
                "collection_write_off_contract_version": (
                    self.collection_write_off_contract_version
                ),
                "collection_write_off_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CollectionWriteOffOutcomeCode.ACCEPTED
            and outcome_text != CollectionWriteOffOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported collection write-off outcome: {outcome_text}")
        payload: dict[str, object] = {
            "collection_write_off_contract_version": self.collection_write_off_contract_version,
            "collection_write_off_outcome_code": outcome_text,
            "collection_write_off_id": str(self.collection_write_off_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "write_off_amount": format_exact_decimal(self.write_off_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_write_off_status": self.collection_write_off_status,
            "collection_case_status": self.collection_case_status,
            "written_off_at": _format_written_off_at(self.written_off_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``write_off.recorded`` facts for the #24 envelope.

        The payload is a reference plus hash, not a payment receipt or
        settlement.  PII, PAN, secrets, and statutory identifiers are omitted.
        Remaining outstanding is the stored exact-zero write-off fact, not
        later-mutated case remaining.
        """
        if self.collection_write_off_id is None or self.collection_case_id is None:
            raise ValueError("rejected collection write-off has no webhook event data")
        payload: dict[str, object] = {
            "collection_write_off_id": str(self.collection_write_off_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "collection_write_off_contract_version": (
                self.collection_write_off_contract_version
            ),
            "currency_code": self.currency_code,
            "write_off_amount": format_exact_decimal(self.write_off_amount),
            "remaining_outstanding_amount": "0",
            "collection_write_off_status": self.collection_write_off_status,
            "written_off_at": _format_written_off_at(self.written_off_at),
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class CollectionWriteOffService:
    """Append-only writer of leftover collection remaining."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def write_off_collection_case(
        self,
        tenant_reference: str,
        collection_case_id: UUID,
        write_off_amount: Decimal | None = None,
        currency_code: str | None = None,
    ) -> CollectionWriteOffResult:
        """Write off remaining outstanding on one same-tenant collection case.

        Replay of the same tenant and ``collection_case_id`` returns the
        stored ``collection_write_off_id`` and does not zero outstanding
        again.  First successful write-off enqueues one
        ``write_off.recorded`` outbox event.  Replay of that write-off
        does not enqueue a second row.  Another tenant cannot see or
        write off that case.  The case stays ``open`` or ``dunning`` so
        #46 can settle at exact zero.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionWriteOffRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_collection_write_off(
            tenant.tenant_account_id, collection_case_id
        )
        if existing is not None:
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(
                    CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
                )
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                CollectionWriteOffOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_write_off_recorded(self.ledger, tenant.tenant_reference, result)
            return result
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_SETTLED)
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        if remaining < 0:
            return _rejected(CollectionWriteOffRejectionReasonCode.OUTSTANDING_NEGATIVE)
        if remaining == 0:
            return _rejected(CollectionWriteOffRejectionReasonCode.OUTSTANDING_ALREADY_ZERO)
        if currency_code is not None and currency_code != collection_case.currency_code:
            return _rejected(CollectionWriteOffRejectionReasonCode.CURRENCY_MISMATCH)
        if write_off_amount is not None and write_off_amount != remaining:
            return _rejected(CollectionWriteOffRejectionReasonCode.WRITE_OFF_AMOUNT_MISMATCH)
        recorded_amount = remaining if write_off_amount is None else write_off_amount
        issued_invoice = self.ledger.find_issued_invoice(
            collection_case.tenant_account_id, collection_case.invoice_draft_id
        )
        issued_invoice_id = (
            issued_invoice.issued_invoice_id if issued_invoice is not None else None
        )
        source_payload_hash = compute_write_off_payload_hash(
            _canonical_write_off_snapshot(collection_case, issued_invoice_id, recorded_amount)
        )
        stored = self.ledger.insert_collection_write_off(
            StoredCollectionWriteOff(
                collection_write_off_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                collection_case_id=collection_case.collection_case_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                issued_invoice_id=issued_invoice_id,
                collection_write_off_contract_version=COLLECTION_WRITE_OFF_CONTRACT_VERSION,
                source_payload_hash=source_payload_hash,
                currency_code=collection_case.currency_code,
                write_off_amount=recorded_amount,
                remaining_outstanding_amount=Decimal("0"),
                collection_write_off_status=COLLECTION_WRITE_OFF_STATUS,
                written_off_at=self._clock(),
            )
        )
        updated_case = self.ledger.apply_collection_write_off(
            collection_case.collection_case_id, recorded_amount
        )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            CollectionWriteOffOutcomeCode.ACCEPTED,
        )
        _enqueue_write_off_recorded(self.ledger, tenant.tenant_reference, result)
        return result


def _canonical_write_off_snapshot(
    collection_case: StoredCollectionCase,
    issued_invoice_id: UUID | None,
    write_off_amount: Decimal,
) -> dict[str, object]:
    """Return case, invoice, currency, write-off amount, zero remaining, and version."""
    payload: dict[str, object] = {
        "collection_case_id": str(collection_case.collection_case_id),
        "invoice_draft_id": str(collection_case.invoice_draft_id),
        "currency_code": collection_case.currency_code,
        "write_off_amount": format_exact_decimal(write_off_amount),
        "remaining_outstanding_amount": "0",
        "collection_write_off_contract_version": COLLECTION_WRITE_OFF_CONTRACT_VERSION,
    }
    if issued_invoice_id is not None:
        payload["issued_invoice_id"] = str(issued_invoice_id)
    return payload


def _enqueue_write_off_recorded(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: CollectionWriteOffResult,
) -> None:
    """Append one ``write_off.recorded`` outbox row for a stored write-off.

    Replay of the same tenant, event type, ``collection_write_off_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next write-off replay.
    """
    if result.collection_write_off_id is None or result.written_off_at is None:
        raise ValueError(
            "accepted collection write-offs must include identity and written_off_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_WRITE_OFF_RECORDED,
        result.collection_write_off_id,
        result.as_webhook_event_data(),
        result.written_off_at,
    )


def _rejected(
    reason_code: CollectionWriteOffRejectionReasonCode,
) -> CollectionWriteOffResult:
    """Build a rejected result without writing a write-off or changing outstanding."""
    return CollectionWriteOffResult(
        collection_write_off_outcome_code=CollectionWriteOffOutcomeCode.REJECTED,
        collection_write_off_contract_version=COLLECTION_WRITE_OFF_CONTRACT_VERSION,
        collection_write_off_id=None,
        collection_case_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        write_off_amount=None,
        remaining_outstanding_amount=None,
        collection_write_off_status=None,
        collection_case_status=None,
        written_off_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredCollectionWriteOff,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: CollectionWriteOffOutcomeCode,
) -> CollectionWriteOffResult:
    """Project a persisted write-off and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return CollectionWriteOffResult(
        collection_write_off_outcome_code=outcome,
        collection_write_off_contract_version=stored.collection_write_off_contract_version,
        collection_write_off_id=stored.collection_write_off_id,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        write_off_amount=stored.write_off_amount,
        remaining_outstanding_amount=remaining,
        collection_write_off_status=stored.collection_write_off_status,
        collection_case_status=collection_case.collection_case_status,
        written_off_at=stored.written_off_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_SETTLE,
        rejection_reason_code=None,
    )


def _format_written_off_at(written_off_at: datetime | None) -> str:
    """Render ``written_off_at`` as a timezone-aware ISO 8601 instant."""
    if written_off_at is None:
        raise ValueError("accepted collection write-offs must include written_off_at")
    return written_off_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
