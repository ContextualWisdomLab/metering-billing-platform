"""Park leftover remittance against a stored payment receipt.

The service is the buyer-facing leftover path:

1. Resolve the tenant and same-tenant payment receipt.
2. Require a positive leftover that does not exceed the stored receipt.
3. Persist one append-only ``unapplied_cash`` row per receipt.
4. Leave the receipt, case remaining, and #12 overpay reject unchanged.

#12 applies the full ``received_amount`` to the case and rejects overpay.
Implied leftover ``receipt_amount - applied_to_case`` is therefore exact
zero unless the operator supplies leftover extra cash.  Omitting the
amount fail-closes as already consumed.  Replay of the same tenant and
``payment_receipt_id`` returns the stored ``unapplied_cash_id``.  The
path does not emit a journal, webhook, write-off, settlement, credit
note, or AIS call.  Apply leftover to another case is a later slice.
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
    UnappliedCashOutcomeCode,
    UnappliedCashRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredPaymentReceipt,
    StoredUnappliedCash,
    generate_record_id,
)


Clock = Callable[[], datetime]
UNAPPLIED_CASH_CONTRACT_VERSION = 1
UNAPPLIED_CASH_STATUS = "parked"
OPERATOR_ACTION_WAIT = "wait"
ZERO = Decimal("0")


def compute_unapplied_cash_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical leftover identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class UnappliedCashResult:
    """Buyer-facing result of parking leftover remittance against one receipt."""

    unapplied_cash_outcome_code: UnappliedCashOutcomeCode
    unapplied_cash_contract_version: int
    unapplied_cash_id: UUID | None
    payment_receipt_id: UUID | None
    payment_intent_id: UUID | None
    collection_case_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    unapplied_amount: Decimal | None
    received_amount: Decimal | None
    applied_amount: Decimal | None
    unapplied_cash_status: str | None
    parked_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: UnappliedCashRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published leftover, or a sparse rejected result."""
        outcome = self.unapplied_cash_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, UnappliedCashOutcomeCode) else str(outcome)
        )
        if outcome_text == UnappliedCashOutcomeCode.REJECTED:
            return {
                "unapplied_cash_contract_version": self.unapplied_cash_contract_version,
                "unapplied_cash_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != UnappliedCashOutcomeCode.ACCEPTED
            and outcome_text != UnappliedCashOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported unapplied cash outcome: {outcome_text}")
        return {
            "unapplied_cash_contract_version": self.unapplied_cash_contract_version,
            "unapplied_cash_outcome_code": outcome_text,
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "tenant_reference": self.tenant_reference,
            "payment_receipt_id": str(self.payment_receipt_id),
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "unapplied_amount": format_exact_decimal(self.unapplied_amount),
            "received_amount": format_exact_decimal(self.received_amount),
            "applied_amount": format_exact_decimal(self.applied_amount),
            "unapplied_cash_status": self.unapplied_cash_status,
            "parked_at": _format_parked_at(self.parked_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }


class UnappliedCashService:
    """Append-only writer of leftover remittance against a stored receipt."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def park_unapplied_cash(
        self,
        tenant_reference: str,
        payment_receipt_id: UUID,
        unapplied_amount: object | None = None,
        currency_code: str | None = None,
    ) -> UnappliedCashResult:
        """Park leftover remittance against one same-tenant payment receipt.

        Replay of the same tenant and ``payment_receipt_id`` returns the
        stored ``unapplied_cash_id`` and does not write a second row.
        #12 still rejects overpay.  Implied leftover from a stored
        receipt is exact zero because the full received amount was
        applied to the case.  A supplied leftover must be a positive
        exact decimal that does not exceed the receipt.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(UnappliedCashRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_unapplied_cash(tenant.tenant_account_id, payment_receipt_id)
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                UnappliedCashOutcomeCode.DUPLICATE_REPLAY,
            )
        payment_receipt = self.ledger.get_payment_receipt(payment_receipt_id)
        if (
            payment_receipt is None
            or payment_receipt.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND)
        if unapplied_amount is None:
            return _rejected(UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_ALREADY_CONSUMED)
        try:
            leftover = _parse_leftover(unapplied_amount)
        except ExactDecimalError:
            return _rejected(UnappliedCashRejectionReasonCode.REQUEST_INVALID)
        if leftover == ZERO:
            return _rejected(UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_ZERO)
        if leftover < ZERO:
            return _rejected(UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_NEGATIVE)
        if leftover > payment_receipt.received_amount:
            return _rejected(UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_EXCEEDS_RECEIPT)
        if currency_code is not None and currency_code != payment_receipt.currency_code:
            return _rejected(UnappliedCashRejectionReasonCode.CURRENCY_MISMATCH)
        applied_amount = payment_receipt.received_amount
        source_payload_hash = compute_unapplied_cash_payload_hash(
            _canonical_unapplied_snapshot(payment_receipt, leftover)
        )
        candidate = StoredUnappliedCash(
            unapplied_cash_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            payment_receipt_id=payment_receipt.payment_receipt_id,
            payment_intent_id=payment_receipt.payment_intent_id,
            collection_case_id=payment_receipt.collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash=source_payload_hash,
            currency_code=payment_receipt.currency_code,
            unapplied_amount=leftover,
            received_amount=payment_receipt.received_amount,
            applied_amount=applied_amount,
            unapplied_cash_status=UNAPPLIED_CASH_STATUS,
            parked_at=self._clock(),
        )
        stored = self.ledger.insert_unapplied_cash(candidate)
        if stored.unapplied_cash_id != candidate.unapplied_cash_id:
            return _from_stored(
                stored,
                tenant.tenant_reference,
                UnappliedCashOutcomeCode.DUPLICATE_REPLAY,
            )
        return _from_stored(
            stored, tenant.tenant_reference, UnappliedCashOutcomeCode.ACCEPTED
        )


def _parse_leftover(value: object) -> Decimal:
    """Parse leftover remittance as an exact decimal without IEEE money."""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ExactDecimalError("leftover must be a finite exact decimal")
        return Decimal(format(value, "f"))
    return parse_exact_decimal(value)


def _canonical_unapplied_snapshot(
    payment_receipt: StoredPaymentReceipt, unapplied_amount: Decimal
) -> dict[str, object]:
    """Return receipt, applied amount, leftover, currency, and version."""
    return {
        "payment_receipt_id": str(payment_receipt.payment_receipt_id),
        "currency_code": payment_receipt.currency_code,
        "unapplied_amount": format_exact_decimal(unapplied_amount),
        "received_amount": format_exact_decimal(payment_receipt.received_amount),
        "applied_amount": format_exact_decimal(payment_receipt.received_amount),
        "unapplied_cash_contract_version": UNAPPLIED_CASH_CONTRACT_VERSION,
    }


def _rejected(reason_code: UnappliedCashRejectionReasonCode) -> UnappliedCashResult:
    """Build a rejected result without writing leftover or changing the receipt."""
    return UnappliedCashResult(
        unapplied_cash_outcome_code=UnappliedCashOutcomeCode.REJECTED,
        unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
        unapplied_cash_id=None,
        payment_receipt_id=None,
        payment_intent_id=None,
        collection_case_id=None,
        tenant_reference=None,
        currency_code=None,
        unapplied_amount=None,
        received_amount=None,
        applied_amount=None,
        unapplied_cash_status=None,
        parked_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredUnappliedCash,
    tenant_reference: str,
    outcome: UnappliedCashOutcomeCode,
) -> UnappliedCashResult:
    """Project a persisted leftover row into the buyer result."""
    return UnappliedCashResult(
        unapplied_cash_outcome_code=outcome,
        unapplied_cash_contract_version=stored.unapplied_cash_contract_version,
        unapplied_cash_id=stored.unapplied_cash_id,
        payment_receipt_id=stored.payment_receipt_id,
        payment_intent_id=stored.payment_intent_id,
        collection_case_id=stored.collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        unapplied_amount=stored.unapplied_amount,
        received_amount=stored.received_amount,
        applied_amount=stored.applied_amount,
        unapplied_cash_status=stored.unapplied_cash_status,
        parked_at=stored.parked_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_parked_at(parked_at: datetime | None) -> str:
    """Render a park timestamp as a timezone-aware ISO 8601 instant."""
    if parked_at is None:
        raise ValueError("accepted unapplied cash must include parked_at")
    return parked_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
