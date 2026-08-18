"""Settle one open collection case whose remaining outstanding is exact zero.

The service is the buyer-facing settle path:

1. Resolve the tenant and same-tenant collection case.
2. Require remaining outstanding to be exact zero.
3. Persist one append-only ``collection_case_settlement`` per case.
4. Flip the case to ``settled`` without inventing a receipt or write-off.

Replay of the same tenant and ``collection_case_id`` returns the stored
settlement and never double-settles.  First successful settle enqueues
one existing ``collection.settled`` outbox event; replay of the same
settlement does not enqueue a second row.  The path does not emit a
journal, unwind tax, capture payment, call AIS, or invent a write-off.
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
    CollectionCaseSettlementOutcomeCode,
    CollectionCaseSettlementRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionCaseSettlement,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_COLLECTION_SETTLED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
COLLECTION_CASE_SETTLEMENT_CONTRACT_VERSION = 1
COLLECTION_CASE_SETTLEMENT_STATUS = "settled"
OPERATOR_ACTION_WAIT = "wait"


def compute_settlement_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical settle identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CollectionCaseSettlementResult:
    """Buyer-facing result of settling one collection case at exact zero."""

    collection_case_settlement_outcome_code: CollectionCaseSettlementOutcomeCode
    collection_case_settlement_contract_version: int
    collection_case_settlement_id: UUID | None
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    remaining_outstanding_amount: Decimal | None
    collection_case_settlement_status: str | None
    collection_case_status: str | None
    settled_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: CollectionCaseSettlementRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published settlement, or a sparse rejected result."""
        outcome = self.collection_case_settlement_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, CollectionCaseSettlementOutcomeCode)
            else str(outcome)
        )
        if outcome_text == CollectionCaseSettlementOutcomeCode.REJECTED:
            return {
                "collection_case_settlement_contract_version": (
                    self.collection_case_settlement_contract_version
                ),
                "collection_case_settlement_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CollectionCaseSettlementOutcomeCode.ACCEPTED
            and outcome_text != CollectionCaseSettlementOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported collection case settlement outcome: {outcome_text}")
        payload: dict[str, object] = {
            "collection_case_settlement_contract_version": (
                self.collection_case_settlement_contract_version
            ),
            "collection_case_settlement_outcome_code": outcome_text,
            "collection_case_settlement_id": str(self.collection_case_settlement_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_case_settlement_status": self.collection_case_settlement_status,
            "collection_case_status": self.collection_case_status,
            "settled_at": _format_settled_at(self.settled_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``collection.settled`` facts for the #24 envelope.

        The payload is a reference plus hash, not a payment receipt or
        write-off.  PII, PAN, secrets, and statutory identifiers are omitted.
        Remaining outstanding is the stored exact-zero settlement fact.
        """
        if self.collection_case_settlement_id is None or self.collection_case_id is None:
            raise ValueError("rejected collection case settlement has no webhook event data")
        payload: dict[str, object] = {
            "collection_case_settlement_id": str(self.collection_case_settlement_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "collection_case_settlement_contract_version": (
                self.collection_case_settlement_contract_version
            ),
            "currency_code": self.currency_code,
            "remaining_outstanding_amount": "0",
            "collection_case_status": self.collection_case_status,
            "settled_at": _format_settled_at(self.settled_at),
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class CollectionCaseSettlementService:
    """Append-only settler of exact-zero collection cases."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def settle_collection_case(
        self, tenant_reference: str, collection_case_id: UUID
    ) -> CollectionCaseSettlementResult:
        """Settle one same-tenant collection case at exact-zero outstanding.

        Replay of the same tenant and ``collection_case_id`` returns the
        stored ``collection_case_settlement_id`` and does not flip status
        again.  First successful settle enqueues one ``collection.settled``
        outbox event.  Replay of that settlement does not enqueue a second
        row.  Another tenant cannot see or settle that case.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CollectionCaseSettlementRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_collection_case_settlement(
            tenant.tenant_account_id, collection_case_id
        )
        if existing is not None:
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(
                    CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
                )
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                CollectionCaseSettlementOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_collection_settled(self.ledger, tenant.tenant_reference, result)
            return result
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(
                CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
            )
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(
                CollectionCaseSettlementRejectionReasonCode.COLLECTION_CASE_SETTLED
            )
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = Decimal("0")
        if remaining != 0:
            return _rejected(
                CollectionCaseSettlementRejectionReasonCode.OUTSTANDING_NOT_ZERO
            )
        issued_invoice = self.ledger.find_issued_invoice(
            collection_case.tenant_account_id, collection_case.invoice_draft_id
        )
        issued_invoice_id = (
            issued_invoice.issued_invoice_id if issued_invoice is not None else None
        )
        source_payload_hash = compute_settlement_payload_hash(
            _canonical_settlement_snapshot(collection_case, issued_invoice_id)
        )
        stored = self.ledger.insert_collection_case_settlement(
            StoredCollectionCaseSettlement(
                collection_case_settlement_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                collection_case_id=collection_case.collection_case_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                issued_invoice_id=issued_invoice_id,
                collection_case_settlement_contract_version=(
                    COLLECTION_CASE_SETTLEMENT_CONTRACT_VERSION
                ),
                source_payload_hash=source_payload_hash,
                currency_code=collection_case.currency_code,
                remaining_outstanding_amount=Decimal("0"),
                collection_case_settlement_status=COLLECTION_CASE_SETTLEMENT_STATUS,
                settled_at=self._clock(),
            )
        )
        updated_case = self.ledger.mark_collection_case_settled(
            collection_case.collection_case_id
        )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            CollectionCaseSettlementOutcomeCode.ACCEPTED,
        )
        _enqueue_collection_settled(self.ledger, tenant.tenant_reference, result)
        return result


def _canonical_settlement_snapshot(
    collection_case: StoredCollectionCase, issued_invoice_id: UUID | None
) -> dict[str, object]:
    """Return case, invoice, currency, zero remaining, and contract version."""
    payload: dict[str, object] = {
        "collection_case_id": str(collection_case.collection_case_id),
        "invoice_draft_id": str(collection_case.invoice_draft_id),
        "currency_code": collection_case.currency_code,
        "remaining_outstanding_amount": "0",
        "collection_case_settlement_contract_version": (
            COLLECTION_CASE_SETTLEMENT_CONTRACT_VERSION
        ),
    }
    if issued_invoice_id is not None:
        payload["issued_invoice_id"] = str(issued_invoice_id)
    return payload


def _enqueue_collection_settled(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: CollectionCaseSettlementResult,
) -> None:
    """Append one ``collection.settled`` outbox row for a stored settlement.

    Replay of the same tenant, event type, ``collection_case_settlement_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next settle replay.
    """
    if result.collection_case_settlement_id is None or result.settled_at is None:
        raise ValueError(
            "accepted collection case settlements must include identity and settled_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_COLLECTION_SETTLED,
        result.collection_case_settlement_id,
        result.as_webhook_event_data(),
        result.settled_at,
    )


def _rejected(
    reason_code: CollectionCaseSettlementRejectionReasonCode,
) -> CollectionCaseSettlementResult:
    """Build a rejected result without writing a settlement or flipping status."""
    return CollectionCaseSettlementResult(
        collection_case_settlement_outcome_code=CollectionCaseSettlementOutcomeCode.REJECTED,
        collection_case_settlement_contract_version=COLLECTION_CASE_SETTLEMENT_CONTRACT_VERSION,
        collection_case_settlement_id=None,
        collection_case_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        remaining_outstanding_amount=None,
        collection_case_settlement_status=None,
        collection_case_status=None,
        settled_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredCollectionCaseSettlement,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: CollectionCaseSettlementOutcomeCode,
) -> CollectionCaseSettlementResult:
    """Project a persisted settlement and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return CollectionCaseSettlementResult(
        collection_case_settlement_outcome_code=outcome,
        collection_case_settlement_contract_version=(
            stored.collection_case_settlement_contract_version
        ),
        collection_case_settlement_id=stored.collection_case_settlement_id,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        remaining_outstanding_amount=remaining,
        collection_case_settlement_status=stored.collection_case_settlement_status,
        collection_case_status=collection_case.collection_case_status,
        settled_at=stored.settled_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_settled_at(settled_at: datetime | None) -> str:
    """Render ``settled_at`` as a timezone-aware ISO 8601 instant."""
    if settled_at is None:
        raise ValueError("accepted collection case settlements must include settled_at")
    return settled_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
