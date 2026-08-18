"""Apply one parked leftover onto one open collection case.

The service is the buyer-facing apply path:

1. Resolve the tenant, parked leftover, and open collection case.
2. Require the same currency and the full parked amount.
3. Reduce ``collection_outstanding`` by the exact applied inclusive amount.
4. Persist one append-only ``unapplied_cash_application`` per leftover.

Replay of the same tenant and ``unapplied_cash_id`` returns the stored
application and never double-reduces.  First successful apply enqueues
one existing ``unapplied_cash.applied`` outbox event; replay of the same
application does not enqueue a second row.  The parked leftover row stays
``parked``; the application identity consumes it.  The path does not
auto-settle, emit a journal, write-off, credit note, or AIS call.
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
    ExactDecimalError,
    UnappliedCashApplicationOutcomeCode,
    UnappliedCashApplicationRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredUnappliedCash,
    StoredUnappliedCashApplication,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_UNAPPLIED_CASH_APPLIED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION = 1
UNAPPLIED_CASH_APPLICATION_STATUS = "applied"
OPERATOR_ACTION_COLLECT = "collect"
OPERATOR_ACTION_SETTLE = "settle"
OPERATOR_ACTION_WAIT = "wait"
ZERO = Decimal("0")


def compute_unapplied_cash_application_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical apply identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class UnappliedCashApplicationResult:
    """Buyer-facing result of applying one parked leftover to a case."""

    unapplied_cash_application_outcome_code: UnappliedCashApplicationOutcomeCode
    unapplied_cash_application_contract_version: int
    unapplied_cash_application_id: UUID | None
    unapplied_cash_id: UUID | None
    collection_case_id: UUID | None
    payment_receipt_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    applied_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    unapplied_cash_application_status: str | None
    collection_case_status: str | None
    applied_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: UnappliedCashApplicationRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published application, or a sparse rejected result."""
        outcome = self.unapplied_cash_application_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, UnappliedCashApplicationOutcomeCode)
            else str(outcome)
        )
        if outcome_text == UnappliedCashApplicationOutcomeCode.REJECTED:
            return {
                "unapplied_cash_application_contract_version": (
                    self.unapplied_cash_application_contract_version
                ),
                "unapplied_cash_application_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != UnappliedCashApplicationOutcomeCode.ACCEPTED
            and outcome_text != UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported unapplied cash application outcome: {outcome_text}")
        return {
            "unapplied_cash_application_contract_version": (
                self.unapplied_cash_application_contract_version
            ),
            "unapplied_cash_application_outcome_code": outcome_text,
            "unapplied_cash_application_id": str(self.unapplied_cash_application_id),
            "tenant_reference": self.tenant_reference,
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "collection_case_id": str(self.collection_case_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "applied_amount": format_exact_decimal(self.applied_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "unapplied_cash_application_status": self.unapplied_cash_application_status,
            "collection_case_status": self.collection_case_status,
            "applied_at": _format_applied_at(self.applied_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }

    def as_webhook_event_data(
        self, issued_invoice_id: UUID | None = None
    ) -> dict[str, object]:
        """Return the thin ``unapplied_cash.applied`` facts for the #24 envelope.

        The payload is a reference plus hash, not a payment receipt or
        settlement.  PII, PAN, secrets, and statutory identifiers are omitted.
        Remaining outstanding is not stored on the application row, so it is
        omitted to keep the outbox payload hash stable across later case
        mutations.
        """
        if self.unapplied_cash_application_id is None or self.unapplied_cash_id is None:
            raise ValueError("rejected unapplied cash application has no webhook event data")
        if self.applied_at is None:
            raise ValueError("accepted unapplied cash applications must include applied_at")
        payload: dict[str, object] = {
            "unapplied_cash_application_id": str(self.unapplied_cash_application_id),
            "unapplied_cash_id": str(self.unapplied_cash_id),
            "payment_receipt_id": str(self.payment_receipt_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "unapplied_cash_application_contract_version": (
                self.unapplied_cash_application_contract_version
            ),
            "currency_code": self.currency_code,
            "applied_amount": format_exact_decimal(self.applied_amount),
            "unapplied_cash_application_status": self.unapplied_cash_application_status,
            "applied_at": _format_applied_at(self.applied_at),
        }
        if issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(issued_invoice_id)
        return payload


class UnappliedCashApplicationService:
    """Append-only applier of parked leftover onto collection cases."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def apply_unapplied_cash(
        self,
        tenant_reference: str,
        unapplied_cash_id: UUID,
        collection_case_id: UUID,
        applied_amount: object | None = None,
        currency_code: str | None = None,
    ) -> UnappliedCashApplicationResult:
        """Apply one same-tenant parked leftover to one open collection case.

        Replay of the same tenant and ``unapplied_cash_id`` returns the
        stored ``unapplied_cash_application_id`` and does not reduce
        outstanding again.  The apply uses the full parked amount.
        Remaining zero does not settle the case.  First successful apply
        enqueues one ``unapplied_cash.applied`` outbox event.  Replay of
        that application does not enqueue a second row.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(UnappliedCashApplicationRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        parked = self.ledger.get_unapplied_cash(unapplied_cash_id)
        if parked is None or parked.tenant_account_id != tenant.tenant_account_id:
            return _rejected(UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND)
        existing = self.ledger.find_unapplied_cash_application(
            tenant.tenant_account_id, parked.unapplied_cash_id
        )
        if existing is not None:
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(
                    UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
                )
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                UnappliedCashApplicationOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_unapplied_cash_applied(self.ledger, tenant.tenant_reference, result)
            return result
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(
                UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND
            )
        if currency_code is not None and currency_code != parked.currency_code:
            return _rejected(UnappliedCashApplicationRejectionReasonCode.CURRENCY_MISMATCH)
        if parked.currency_code != collection_case.currency_code:
            return _rejected(UnappliedCashApplicationRejectionReasonCode.CURRENCY_MISMATCH)
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(
                UnappliedCashApplicationRejectionReasonCode.COLLECTION_CASE_SETTLED
            )
        remaining = collection_case.outstanding_amount
        if remaining < ZERO:
            return _rejected(UnappliedCashApplicationRejectionReasonCode.OUTSTANDING_NEGATIVE)
        if applied_amount is None:
            leftover = parked.unapplied_amount
        else:
            try:
                leftover = _parse_applied_amount(applied_amount)
            except ExactDecimalError:
                return _rejected(UnappliedCashApplicationRejectionReasonCode.REQUEST_INVALID)
            if leftover == ZERO:
                return _rejected(UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_ZERO)
            if leftover < ZERO:
                return _rejected(
                    UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_NEGATIVE
                )
            if leftover > parked.unapplied_amount:
                return _rejected(
                    UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_EXCEEDS_PARKED
                )
            if leftover != parked.unapplied_amount:
                return _rejected(
                    UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_MISMATCH
                )
        if leftover > remaining:
            return _rejected(
                UnappliedCashApplicationRejectionReasonCode.APPLIED_AMOUNT_EXCEEDS_OUTSTANDING
            )
        source_payload_hash = compute_unapplied_cash_application_payload_hash(
            _canonical_application_snapshot(parked, collection_case, leftover)
        )
        stored = self.ledger.insert_unapplied_cash_application(
            StoredUnappliedCashApplication(
                unapplied_cash_application_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                unapplied_cash_id=parked.unapplied_cash_id,
                collection_case_id=collection_case.collection_case_id,
                payment_receipt_id=parked.payment_receipt_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                unapplied_cash_application_contract_version=(
                    UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION
                ),
                source_payload_hash=source_payload_hash,
                currency_code=parked.currency_code,
                applied_amount=leftover,
                unapplied_cash_application_status=UNAPPLIED_CASH_APPLICATION_STATUS,
                applied_at=self._clock(),
            )
        )
        updated_case = self.ledger.apply_unapplied_cash_to_collection_case(
            collection_case.collection_case_id, leftover
        )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            UnappliedCashApplicationOutcomeCode.ACCEPTED,
        )
        _enqueue_unapplied_cash_applied(self.ledger, tenant.tenant_reference, result)
        return result


def _enqueue_unapplied_cash_applied(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: UnappliedCashApplicationResult,
) -> None:
    """Append one ``unapplied_cash.applied`` outbox row for a stored apply.

    Replay of the same tenant, event type, ``unapplied_cash_application_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next apply replay.
    """
    if result.unapplied_cash_application_id is None or result.applied_at is None:
        raise ValueError(
            "accepted unapplied cash applications must include identity and applied_at"
        )
    issued_invoice_id = None
    if result.collection_case_id is not None and result.invoice_draft_id is not None:
        collection_case = ledger.get_collection_case(result.collection_case_id)
        if collection_case is not None:
            issued = ledger.find_issued_invoice(
                collection_case.tenant_account_id, result.invoice_draft_id
            )
            if issued is not None:
                issued_invoice_id = issued.issued_invoice_id
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_UNAPPLIED_CASH_APPLIED,
        result.unapplied_cash_application_id,
        result.as_webhook_event_data(issued_invoice_id),
        result.applied_at,
    )


def _parse_applied_amount(value: object) -> Decimal:
    """Parse an applied leftover as an exact decimal without IEEE money."""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ExactDecimalError("applied leftover must be a finite exact decimal")
        return Decimal(format(value, "f"))
    return parse_exact_decimal(value)


def _canonical_application_snapshot(
    parked: StoredUnappliedCash,
    collection_case: StoredCollectionCase,
    applied_amount: Decimal,
) -> dict[str, object]:
    """Return leftover, case, receipt, currency, amount, and version."""
    return {
        "unapplied_cash_id": str(parked.unapplied_cash_id),
        "collection_case_id": str(collection_case.collection_case_id),
        "payment_receipt_id": str(parked.payment_receipt_id),
        "currency_code": parked.currency_code,
        "applied_amount": format_exact_decimal(applied_amount),
        "unapplied_amount": format_exact_decimal(parked.unapplied_amount),
        "unapplied_cash_application_contract_version": (
            UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION
        ),
    }


def _rejected(
    reason_code: UnappliedCashApplicationRejectionReasonCode,
) -> UnappliedCashApplicationResult:
    """Build a rejected result without writing an application or reducing money."""
    return UnappliedCashApplicationResult(
        unapplied_cash_application_outcome_code=UnappliedCashApplicationOutcomeCode.REJECTED,
        unapplied_cash_application_contract_version=UNAPPLIED_CASH_APPLICATION_CONTRACT_VERSION,
        unapplied_cash_application_id=None,
        unapplied_cash_id=None,
        collection_case_id=None,
        payment_receipt_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        currency_code=None,
        applied_amount=None,
        remaining_outstanding_amount=None,
        unapplied_cash_application_status=None,
        collection_case_status=None,
        applied_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredUnappliedCashApplication,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: UnappliedCashApplicationOutcomeCode,
) -> UnappliedCashApplicationResult:
    """Project a persisted application and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return UnappliedCashApplicationResult(
        unapplied_cash_application_outcome_code=outcome,
        unapplied_cash_application_contract_version=(
            stored.unapplied_cash_application_contract_version
        ),
        unapplied_cash_application_id=stored.unapplied_cash_application_id,
        unapplied_cash_id=stored.unapplied_cash_id,
        collection_case_id=stored.collection_case_id,
        payment_receipt_id=stored.payment_receipt_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        applied_amount=stored.applied_amount,
        remaining_outstanding_amount=remaining,
        unapplied_cash_application_status=stored.unapplied_cash_application_status,
        collection_case_status=collection_case.collection_case_status,
        applied_at=stored.applied_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=_next_operator_action(collection_case, remaining),
        rejection_reason_code=None,
    )


def _next_operator_action(collection_case: StoredCollectionCase, remaining: Decimal) -> str:
    """Point operators at collect, explicit settle, or wait."""
    if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
        return OPERATOR_ACTION_WAIT
    if remaining == ZERO:
        return OPERATOR_ACTION_SETTLE
    return OPERATOR_ACTION_COLLECT


def _format_applied_at(applied_at: datetime | None) -> str:
    """Render ``applied_at`` as a timezone-aware ISO 8601 instant."""
    if applied_at is None:
        raise ValueError("accepted unapplied cash application must include applied_at")
    return applied_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
