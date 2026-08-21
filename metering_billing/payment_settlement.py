"""Commercial payment receipts applied against stored payment intents.

The service is the buyer-facing settlement path:

1. Resolve the tenant.
2. Load that tenant's stored ``payment_intent``.
3. Apply an exact received amount as one append-only ``payment_receipt``.
4. Reduce the linked collection-case outstanding by the same amount.
5. Idempotently propose the existing cash journal for that receipt.
6. Replay the same tenant, intent, payload hash, and contract version.

ISO 20022 distinguishes payment initiation from settlement.  This path records
commercial application of cash against a projected intent; it does not project
an ISO 20022 settlement message, capture via a named provider, or post a
journal (International Organization for Standardization, 2026).  PCI DSS scope
is reduced by never storing a card PAN (PCI Security Standards Council, 2024).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.accounting_export import AccountingExportService
from metering_billing.errors import (
    ExactDecimalError,
    PaymentSettlementOutcomeCode,
    PaymentSettlementRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.payment_intent import parse_payment_amount
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCollectionDunningEvent,
    StoredPaymentIntent,
    StoredPaymentReceipt,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
SETTLEMENT_CONTRACT_VERSION = 1
PAYMENT_RECEIPT_STATUS = "applied"
PROJECTED_INTENT_STATUS = "projected"
CANCELLED_INTENT_STATUS = "cancelled"
CASH_JOURNAL_ACTION = "The cash journal is already validated for AIS to pull."
PARTIAL_RECEIPT_ACTION = (
    "The cash journal is already validated for AIS to pull, or record another partial receipt."
)
CANCEL_REPLACEMENT_ACTION = "Project a replacement payment_intent if collection should continue."


def parse_settlement_amount(value: Any) -> Decimal:
    """Parse a received settlement amount as an exact non-negative decimal.

    Reuses payment-intent money rules so receipts and intents reject the same
    IEEE binary floats and non-canonical values.
    """
    return parse_payment_amount(value)


def compute_settlement_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical receipt identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class PaymentSettlementResult:
    """Buyer-facing result of applying a receipt or cancelling an intent."""

    payment_settlement_outcome_code: PaymentSettlementOutcomeCode
    settlement_contract_version: int
    payment_receipt_id: UUID | None
    payment_intent_id: UUID | None
    collection_case_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    payment_receipt_status: str | None
    payment_intent_status: str | None
    received_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    collection_case_status: str | None
    source_payload_hash: str | None
    received_at: datetime | None
    next_operator_action: str
    rejection_reason_code: PaymentSettlementRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published receipt, a cancel acknowledgement, or a sparse reject."""
        outcome = self.payment_settlement_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, PaymentSettlementOutcomeCode)
            else str(outcome)
        )
        if outcome_text == PaymentSettlementOutcomeCode.REJECTED:
            return {
                "settlement_contract_version": self.settlement_contract_version,
                "payment_settlement_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else "payment_intent_not_found"
                ),
            }
        if (
            outcome_text != PaymentSettlementOutcomeCode.ACCEPTED
            and outcome_text != PaymentSettlementOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported payment settlement outcome: {outcome_text}")
        if self.payment_receipt_id is None:
            return {
                "settlement_contract_version": self.settlement_contract_version,
                "payment_settlement_outcome_code": outcome_text,
                "payment_intent_id": str(self.payment_intent_id),
                "tenant_reference": self.tenant_reference,
                "collection_case_id": str(self.collection_case_id),
                "currency_code": self.currency_code,
                "payment_intent_status": self.payment_intent_status,
                "remaining_outstanding_amount": format_exact_decimal(
                    self.remaining_outstanding_amount
                ),
                "collection_case_status": self.collection_case_status,
                "next_operator_action": self.next_operator_action,
            }
        if self.received_at is None:
            raise ValueError("accepted payment receipts must include received_at")
        return {
            "settlement_contract_version": self.settlement_contract_version,
            "payment_settlement_outcome_code": outcome_text,
            "payment_receipt_id": str(self.payment_receipt_id),
            "tenant_reference": self.tenant_reference,
            "payment_intent_id": str(self.payment_intent_id),
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "payment_receipt_status": self.payment_receipt_status,
            "received_amount": format_exact_decimal(self.received_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "collection_case_status": self.collection_case_status,
            "source_payload_hash": self.source_payload_hash,
            "received_at": _format_received_at(self.received_at),
            "next_operator_action": self.next_operator_action,
        }


class PaymentSettlementService:
    """Append-only receipt recorder and projected-intent canceller."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def record_payment_receipt(
        self,
        tenant_reference: str,
        payment_intent_id: UUID,
        received_amount: object,
    ) -> PaymentSettlementResult:
        """Apply one receipt inside the repository transaction boundary."""
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._record_payment_receipt(
                tenant_reference, payment_intent_id, received_amount
            )
        with transaction():
            return self._record_payment_receipt(
                tenant_reference, payment_intent_id, received_amount
            )

    def _record_payment_receipt(
        self,
        tenant_reference: str,
        payment_intent_id: UUID,
        received_amount: object,
    ) -> PaymentSettlementResult:
        """Apply one commercial receipt against a projected payment intent.

        A replay of the same tenant, intent, received amount, source-payload
        hash, and contract version returns the stored ``payment_receipt_id``
        and reuses the stored cash ``proposal_id``.  Another tenant cannot
        see or settle that intent.  The cash journal is already validated
        for AIS to pull.  This service never captures via a provider or
        posts to AIS.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(PaymentSettlementRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        payment_intent = self.ledger.get_payment_intent(payment_intent_id)
        if (
            payment_intent is None
            or payment_intent.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)
        if payment_intent.payment_intent_status != PROJECTED_INTENT_STATUS:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_PROJECTED)

        try:
            parsed_amount = parse_settlement_amount(received_amount)
        except ExactDecimalError:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_INVALID)
        if parsed_amount <= 0:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_INVALID)

        source_payload_hash = compute_settlement_payload_hash(
            _canonical_receipt_snapshot(payment_intent, parsed_amount)
        )
        existing = self.ledger.find_payment_receipt(
            tenant.tenant_account_id,
            payment_intent.payment_intent_id,
            source_payload_hash,
            SETTLEMENT_CONTRACT_VERSION,
        )
        if existing is not None:
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)
            _compose_cash_journal(self.ledger, tenant.tenant_reference, existing.payment_receipt_id)
            return _from_receipt(
                existing,
                payment_intent,
                current_case,
                tenant.tenant_reference,
                PaymentSettlementOutcomeCode.DUPLICATE_REPLAY,
                self.ledger.list_collection_dunning_events(current_case.collection_case_id),
            )

        collection_case = self.ledger.get_collection_case(payment_intent.collection_case_id)
        if collection_case is None:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)
        if collection_case.collection_case_status == "disputed":
            return _rejected(PaymentSettlementRejectionReasonCode.COLLECTION_CASE_DISPUTED)
        if parsed_amount > collection_case.outstanding_amount:
            return _rejected(
                PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_EXCEEDS_OUTSTANDING
            )

        candidate_payment_receipt_id = generate_record_id()
        stored = self.ledger.insert_payment_receipt(
            StoredPaymentReceipt(
                payment_receipt_id=candidate_payment_receipt_id,
                tenant_account_id=tenant.tenant_account_id,
                payment_intent_id=payment_intent.payment_intent_id,
                collection_case_id=payment_intent.collection_case_id,
                settlement_contract_version=SETTLEMENT_CONTRACT_VERSION,
                currency_code=payment_intent.currency_code,
                payment_receipt_status=PAYMENT_RECEIPT_STATUS,
                received_amount=parsed_amount,
                source_payload_hash=source_payload_hash,
                received_at=self._clock(),
            )
        )
        if stored.payment_receipt_id != candidate_payment_receipt_id:
            current_case = self.ledger.get_collection_case(stored.collection_case_id)
            if current_case is None:
                return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)
            _compose_cash_journal(self.ledger, tenant.tenant_reference, stored.payment_receipt_id)
            return _from_receipt(
                stored,
                payment_intent,
                current_case,
                tenant.tenant_reference,
                PaymentSettlementOutcomeCode.DUPLICATE_REPLAY,
                self.ledger.list_collection_dunning_events(current_case.collection_case_id),
            )
        updated_case = self.ledger.apply_collection_settlement(
            payment_intent.collection_case_id, parsed_amount
        )
        result = _from_receipt(
            stored,
            payment_intent,
            updated_case,
            tenant.tenant_reference,
            PaymentSettlementOutcomeCode.ACCEPTED,
            self.ledger.list_collection_dunning_events(updated_case.collection_case_id),
        )
        enqueue_accepted_fact(
            self.ledger,
            tenant.tenant_reference,
            EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,
            stored.payment_receipt_id,
            result.as_contract_dict(),
            stored.received_at,
        )
        _compose_cash_journal(self.ledger, tenant.tenant_reference, stored.payment_receipt_id)
        return result

    def cancel_payment_intent(
        self, tenant_reference: str, payment_intent_id: UUID
    ) -> PaymentSettlementResult:
        """Cancel a projected intent without writing a receipt or changing outstanding.

        A replay of the same tenant and already-cancelled intent returns
        ``duplicate_replay``.  A cancelled intent cannot later receive a receipt.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(PaymentSettlementRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        payment_intent = self.ledger.get_payment_intent(payment_intent_id)
        if (
            payment_intent is None
            or payment_intent.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)

        if payment_intent.payment_intent_status == CANCELLED_INTENT_STATUS:
            collection_case = self.ledger.get_collection_case(payment_intent.collection_case_id)
            if collection_case is None:
                return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)
            return _from_cancel(
                payment_intent,
                collection_case,
                tenant.tenant_reference,
                PaymentSettlementOutcomeCode.DUPLICATE_REPLAY,
                self.ledger.list_collection_dunning_events(collection_case.collection_case_id),
            )
        if payment_intent.payment_intent_status != PROJECTED_INTENT_STATUS:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_PROJECTED)

        collection_case = self.ledger.get_collection_case(payment_intent.collection_case_id)
        if collection_case is None:
            return _rejected(PaymentSettlementRejectionReasonCode.PAYMENT_INTENT_NOT_FOUND)

        cancelled = self.ledger.cancel_stored_payment_intent(payment_intent.payment_intent_id)
        return _from_cancel(
            cancelled,
            collection_case,
            tenant.tenant_reference,
            PaymentSettlementOutcomeCode.ACCEPTED,
            self.ledger.list_collection_dunning_events(collection_case.collection_case_id),
        )


def _canonical_receipt_snapshot(
    payment_intent: StoredPaymentIntent, received_amount: Decimal
) -> dict[str, object]:
    """Return intent amount, currency, status, and received amount for identity."""
    return {
        "payment_intent_id": str(payment_intent.payment_intent_id),
        "payment_amount": format_exact_decimal(payment_intent.payment_amount),
        "currency_code": payment_intent.currency_code,
        "payment_intent_status": payment_intent.payment_intent_status,
        "received_amount": format_exact_decimal(received_amount),
        "settlement_contract_version": SETTLEMENT_CONTRACT_VERSION,
    }


def _rejected(reason_code: PaymentSettlementRejectionReasonCode) -> PaymentSettlementResult:
    """Build a rejected result without writing a receipt or changing outstanding."""
    return PaymentSettlementResult(
        payment_settlement_outcome_code=PaymentSettlementOutcomeCode.REJECTED,
        settlement_contract_version=SETTLEMENT_CONTRACT_VERSION,
        payment_receipt_id=None,
        payment_intent_id=None,
        collection_case_id=None,
        tenant_reference=None,
        currency_code=None,
        payment_receipt_status=None,
        payment_intent_status=None,
        received_amount=None,
        remaining_outstanding_amount=None,
        collection_case_status=None,
        source_payload_hash=None,
        received_at=None,
        next_operator_action="",
        rejection_reason_code=reason_code,
    )


def _compose_cash_journal(
    ledger: MemoryUsageLedger, tenant_reference: str, payment_receipt_id: UUID
) -> None:
    """Idempotently propose the existing #13 cash journal for one receipt.

    Replay of the same tenant, receipt, hash, and contract version writes no
    second proposal and does not flip ``proposal_status``.
    """
    AccountingExportService(ledger).propose_cash_journal(tenant_reference, payment_receipt_id)


def _next_receipt_action(remaining_outstanding_amount: Decimal) -> str:
    """Tell the operator the cash journal is validated, or apply another partial."""
    if remaining_outstanding_amount == 0:
        return CASH_JOURNAL_ACTION
    return PARTIAL_RECEIPT_ACTION


def _buyer_collection_case_status(
    collection_case: StoredCollectionCase,
    dunning_events: tuple[StoredCollectionDunningEvent, ...],
) -> str:
    """Prefer settled over dunning so a paid case does not reopen as a reminder."""
    if collection_case.collection_case_status == "settled":
        return "settled"
    if dunning_events:
        return "dunning"
    return collection_case.collection_case_status


def _from_receipt(
    stored: StoredPaymentReceipt,
    payment_intent: StoredPaymentIntent,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: PaymentSettlementOutcomeCode,
    dunning_events: tuple[StoredCollectionDunningEvent, ...],
) -> PaymentSettlementResult:
    """Project a persisted receipt and current case balance into the buyer result."""
    return PaymentSettlementResult(
        payment_settlement_outcome_code=outcome,
        settlement_contract_version=stored.settlement_contract_version,
        payment_receipt_id=stored.payment_receipt_id,
        payment_intent_id=stored.payment_intent_id,
        collection_case_id=stored.collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        payment_receipt_status=stored.payment_receipt_status,
        payment_intent_status=payment_intent.payment_intent_status,
        received_amount=stored.received_amount,
        remaining_outstanding_amount=collection_case.outstanding_amount,
        collection_case_status=_buyer_collection_case_status(collection_case, dunning_events),
        source_payload_hash=stored.source_payload_hash,
        received_at=stored.received_at,
        next_operator_action=_next_receipt_action(collection_case.outstanding_amount),
        rejection_reason_code=None,
    )


def _from_cancel(
    payment_intent: StoredPaymentIntent,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: PaymentSettlementOutcomeCode,
    dunning_events: tuple[StoredCollectionDunningEvent, ...],
) -> PaymentSettlementResult:
    """Project a cancelled intent without a receipt."""
    return PaymentSettlementResult(
        payment_settlement_outcome_code=outcome,
        settlement_contract_version=SETTLEMENT_CONTRACT_VERSION,
        payment_receipt_id=None,
        payment_intent_id=payment_intent.payment_intent_id,
        collection_case_id=payment_intent.collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=payment_intent.currency_code,
        payment_receipt_status=None,
        payment_intent_status=payment_intent.payment_intent_status,
        received_amount=None,
        remaining_outstanding_amount=collection_case.outstanding_amount,
        collection_case_status=_buyer_collection_case_status(collection_case, dunning_events),
        source_payload_hash=None,
        received_at=None,
        next_operator_action=CANCEL_REPLACEMENT_ACTION,
        rejection_reason_code=None,
    )


def _format_received_at(received_at: datetime) -> str:
    """Render ``received_at`` as a timezone-aware ISO 8601 instant."""
    return received_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
