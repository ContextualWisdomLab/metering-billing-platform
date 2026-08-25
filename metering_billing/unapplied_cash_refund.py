"""Refund one parked leftover as a commercial fact.

The service is the buyer-facing leftover-refund path:

1. Resolve the tenant and same-tenant parked leftover.
2. Require the leftover still be parked and not already applied.
3. Persist one append-only ``unapplied_cash_refund`` per leftover.
4. Leave the parked leftover row, receipt, journals, and outbox unchanged.

Replay of the same tenant and ``unapplied_cash_id`` returns the stored
refund and never writes a second row.  First successful refund enqueues
one existing ``refund.recorded`` outbox event; replay of the same
refund does not enqueue a second row.  The path does not capture cards,
call a PSP, emit a journal, write-off, settlement, credit note, or AIS
call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    UnappliedCashRefundOutcomeCode,
    UnappliedCashRefundRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.unapplied_cash import UNAPPLIED_CASH_STATUS
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredUnappliedCash,
    StoredUnappliedCashRefund,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_REFUND_RECORDED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
UNAPPLIED_CASH_REFUND_CONTRACT_VERSION = 1
UNAPPLIED_CASH_REFUND_STATUS = "recorded"
OPERATOR_ACTION_WAIT = "wait"
ZERO = Decimal("0")


def compute_unapplied_cash_refund_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical refund identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class UnappliedCashRefundResult:
    """Buyer-facing result of refunding one parked leftover."""

    unapplied_cash_refund_outcome_code: UnappliedCashRefundOutcomeCode
    unapplied_cash_refund_contract_version: int
    unapplied_cash_refund_id: UUID | None
    unapplied_cash_id: UUID | None
    payment_receipt_id: UUID | None
    payment_intent_id: UUID | None
    collection_case_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    refund_amount: Decimal | None
    unapplied_amount: Decimal | None
    unapplied_cash_refund_status: str | None
    unapplied_cash_status: str | None
    refunded_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: UnappliedCashRefundRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published refund, or a sparse rejected result."""
        outcome = self.unapplied_cash_refund_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, UnappliedCashRefundOutcomeCode)
            else str(outcome)
        )
        if outcome_text == UnappliedCashRefundOutcomeCode.REJECTED:
            return {
                "unapplied_cash_refund_contract_version": (
                    self.unapplied_cash_refund_contract_version
                ),
                "unapplied_cash_refund_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != UnappliedCashRefundOutcomeCode.ACCEPTED
            and outcome_text != UnappliedCashRefundOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported unapplied cash refund outcome: {outcome_text}")
        return {
            "unapplied_cash_refund_contract_version": (
                self.unapplied_cash_refund_contract_version
            ),
            "unapplied_cash_refund_outcome_code": outcome_text,
            "unapplied_cash_refund_id": str(self.unapplied_cash_refund_id),
            "tenant_reference": self.tenant_reference,
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "refund_amount": format_exact_decimal(self.refund_amount),
            "unapplied_amount": format_exact_decimal(self.unapplied_amount),
            "unapplied_cash_refund_status": self.unapplied_cash_refund_status,
            "unapplied_cash_status": self.unapplied_cash_status,
            "refunded_at": _format_refunded_at(self.refunded_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``refund.recorded`` facts for the #24 envelope.

        The payload is a reference plus hash, not a PSP capture or cash
        movement.  PII, PAN, secrets, and statutory identifiers are omitted.
        Payment-intent, collection-case, parked leftover snapshot, and
        leftover status are omitted because they are not required to
        identify the commercial refund fact.
        """
        if self.unapplied_cash_refund_id is None or self.unapplied_cash_id is None:
            raise ValueError("rejected unapplied cash refund has no webhook event data")
        if self.refunded_at is None:
            raise ValueError("accepted unapplied cash refunds must include refunded_at")
        return {
            "unapplied_cash_refund_id": str(self.unapplied_cash_refund_id),
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "source_payload_hash": self.source_payload_hash,
            "unapplied_cash_refund_contract_version": (
                self.unapplied_cash_refund_contract_version
            ),
            "currency_code": self.currency_code,
            "refund_amount": format_exact_decimal(self.refund_amount),
            "unapplied_cash_refund_status": self.unapplied_cash_refund_status,
            "refunded_at": _format_refunded_at(self.refunded_at),
        }


class UnappliedCashRefundService:
    """Append-only writer of commercial leftover refunds."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def refund_unapplied_cash(
        self,
        tenant_reference: str,
        unapplied_cash_id: UUID,
        refund_amount: object | None = None,
        currency_code: str | None = None,
    ) -> UnappliedCashRefundResult:
        """Refund one same-tenant parked leftover as a commercial fact.

        Replay of the same tenant and ``unapplied_cash_id`` returns the
        stored ``unapplied_cash_refund_id`` and does not write a second
        row.  The refund uses the full parked amount.  The parked leftover
        row stays ``parked``; refund uniqueness consumes it.  First
        successful refund enqueues one ``refund.recorded`` outbox event.
        Replay of that refund does not enqueue a second row.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(UnappliedCashRefundRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_unapplied_cash_refund(
            tenant.tenant_account_id, unapplied_cash_id
        )
        if existing is not None:
            leftover = self.ledger.get_unapplied_cash(existing.unapplied_cash_id)
            if leftover is None:
                return _rejected(
                    UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND
                )
            result = _from_stored(
                existing,
                leftover,
                tenant.tenant_reference,
                UnappliedCashRefundOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_refund_recorded(self.ledger, tenant.tenant_reference, result)
            return result
        leftover = self.ledger.get_unapplied_cash(unapplied_cash_id)
        if leftover is None or leftover.tenant_account_id != tenant.tenant_account_id:
            return _rejected(UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND)
        if leftover.unapplied_cash_status != UNAPPLIED_CASH_STATUS:
            return _rejected(UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_PARKED)
        applied = self.ledger.find_unapplied_cash_application(
            tenant.tenant_account_id, leftover.unapplied_cash_id
        )
        if applied is not None:
            return _rejected(
                UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_ALREADY_APPLIED
            )
        if currency_code is not None and currency_code != leftover.currency_code:
            return _rejected(UnappliedCashRefundRejectionReasonCode.CURRENCY_MISMATCH)
        if refund_amount is None:
            recorded_amount = leftover.unapplied_amount
        else:
            try:
                recorded_amount = _parse_refund_amount(refund_amount)
            except ExactDecimalError:
                return _rejected(UnappliedCashRefundRejectionReasonCode.REQUEST_INVALID)
            if recorded_amount == ZERO:
                return _rejected(UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_ZERO)
            if recorded_amount < ZERO:
                return _rejected(UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_NEGATIVE)
            if recorded_amount != leftover.unapplied_amount:
                return _rejected(UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_MISMATCH)
        source_payload_hash = compute_unapplied_cash_refund_payload_hash(
            _canonical_refund_snapshot(leftover, recorded_amount)
        )
        stored = self.ledger.insert_unapplied_cash_refund(
            StoredUnappliedCashRefund(
                unapplied_cash_refund_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                unapplied_cash_id=leftover.unapplied_cash_id,
                payment_receipt_id=leftover.payment_receipt_id,
                payment_intent_id=leftover.payment_intent_id,
                collection_case_id=leftover.collection_case_id,
                unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
                source_payload_hash=source_payload_hash,
                currency_code=leftover.currency_code,
                refund_amount=recorded_amount,
                unapplied_amount=leftover.unapplied_amount,
                unapplied_cash_refund_status=UNAPPLIED_CASH_REFUND_STATUS,
                refunded_at=self._clock(),
            )
        )
        result = _from_stored(
            stored,
            leftover,
            tenant.tenant_reference,
            UnappliedCashRefundOutcomeCode.ACCEPTED,
        )
        _enqueue_refund_recorded(self.ledger, tenant.tenant_reference, result)
        return result


def _enqueue_refund_recorded(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: UnappliedCashRefundResult,
) -> None:
    """Append one ``refund.recorded`` outbox row for a stored refund.

    Replay of the same tenant, event type, ``unapplied_cash_refund_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next refund replay.
    """
    if result.unapplied_cash_refund_id is None or result.refunded_at is None:
        raise ValueError(
            "accepted unapplied cash refunds must include identity and refunded_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_REFUND_RECORDED,
        result.unapplied_cash_refund_id,
        result.as_webhook_event_data(),
        result.refunded_at,
    )


def _parse_refund_amount(value: object) -> Decimal:
    """Parse a leftover refund as an exact decimal without IEEE money."""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ExactDecimalError("refund leftover must be a finite exact decimal")
        return Decimal(format(value, "f"))
    return parse_exact_decimal(value)


def _canonical_refund_snapshot(
    leftover: StoredUnappliedCash, refund_amount: Decimal
) -> dict[str, object]:
    """Return leftover, receipt, currency, amounts, and version."""
    return {
        "unapplied_cash_id": str(leftover.unapplied_cash_id),
        "payment_receipt_id": str(leftover.payment_receipt_id),
        "currency_code": leftover.currency_code,
        "refund_amount": format_exact_decimal(refund_amount),
        "unapplied_amount": format_exact_decimal(leftover.unapplied_amount),
        "unapplied_cash_refund_contract_version": UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
    }


def _rejected(
    reason_code: UnappliedCashRefundRejectionReasonCode,
) -> UnappliedCashRefundResult:
    """Build a rejected result without writing a refund or changing leftover."""
    return UnappliedCashRefundResult(
        unapplied_cash_refund_outcome_code=UnappliedCashRefundOutcomeCode.REJECTED,
        unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
        unapplied_cash_refund_id=None,
        unapplied_cash_id=None,
        payment_receipt_id=None,
        payment_intent_id=None,
        collection_case_id=None,
        tenant_reference=None,
        currency_code=None,
        refund_amount=None,
        unapplied_amount=None,
        unapplied_cash_refund_status=None,
        unapplied_cash_status=None,
        refunded_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredUnappliedCashRefund,
    leftover: StoredUnappliedCash,
    tenant_reference: str,
    outcome: UnappliedCashRefundOutcomeCode,
) -> UnappliedCashRefundResult:
    """Project a persisted refund and the current leftover into the result."""
    return UnappliedCashRefundResult(
        unapplied_cash_refund_outcome_code=outcome,
        unapplied_cash_refund_contract_version=stored.unapplied_cash_refund_contract_version,
        unapplied_cash_refund_id=stored.unapplied_cash_refund_id,
        unapplied_cash_id=stored.unapplied_cash_id,
        payment_receipt_id=stored.payment_receipt_id,
        payment_intent_id=stored.payment_intent_id,
        collection_case_id=stored.collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        refund_amount=stored.refund_amount,
        unapplied_amount=stored.unapplied_amount,
        unapplied_cash_refund_status=stored.unapplied_cash_refund_status,
        unapplied_cash_status=leftover.unapplied_cash_status,
        refunded_at=stored.refunded_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_refunded_at(refunded_at: datetime | None) -> str:
    """Render ``refunded_at`` as a timezone-aware ISO 8601 instant."""
    if refunded_at is None:
        raise ValueError("accepted unapplied cash refund must include refunded_at")
    return refunded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
