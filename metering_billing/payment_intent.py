"""Provider-neutral payment intents produced from stored collection cases.

The service is the buyer-facing projection path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_case``.
3. Copy the exact outstanding into one projected payment intent.
4. Replay the same tenant, case, payload hash, and contract version.

The intent is a commercial initiation record, not a capture, settlement, or
posted journal.  ISO 20022 payment initiation stays provider-neutral, and
PCI DSS scope is reduced by never storing a card PAN (PCI Security Standards
Council, 2024; International Organization for Standardization, 2026).
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
    PaymentIntentOutcomeCode,
    PaymentIntentRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredPaymentIntent,
    generate_record_id,
)


Clock = Callable[[], datetime]
PAYMENT_INTENT_CONTRACT_VERSION = 1
PAYMENT_INTENT_STATUS = "projected"


def parse_payment_amount(value: Any) -> Decimal:
    """Parse a payment-intent amount as an exact non-negative decimal.

    Binary floating-point values are rejected at this boundary so an intent
    cannot smuggle IEEE inexact money into a projected collection amount.
    """
    if isinstance(value, Decimal):
        return parse_exact_decimal(format_exact_decimal(value))
    return parse_exact_decimal(value)


def compute_payment_intent_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical case snapshot."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class PaymentIntentResult:
    """Buyer-facing result of projecting one tenant collection case."""

    payment_intent_outcome_code: PaymentIntentOutcomeCode
    payment_intent_contract_version: int
    payment_intent_id: UUID | None
    collection_case_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    payment_intent_status: str | None
    payment_amount: Decimal | None
    source_payload_hash: str | None
    projected_at: datetime | None
    rejection_reason_code: PaymentIntentRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published payment intent, or a sparse rejected result."""
        outcome = self.payment_intent_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, PaymentIntentOutcomeCode) else str(outcome)
        )
        if outcome_text == PaymentIntentOutcomeCode.REJECTED:
            return {
                "payment_intent_contract_version": self.payment_intent_contract_version,
                "payment_intent_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else "collection_case_not_found"
                ),
            }
        if (
            outcome_text != PaymentIntentOutcomeCode.ACCEPTED
            and outcome_text != PaymentIntentOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported payment intent outcome: {outcome_text}")
        if self.projected_at is None:
            raise ValueError("accepted payment intents must include projected_at")
        return {
            "payment_intent_contract_version": self.payment_intent_contract_version,
            "payment_intent_outcome_code": outcome_text,
            "payment_intent_id": str(self.payment_intent_id),
            "tenant_reference": self.tenant_reference,
            "collection_case_id": str(self.collection_case_id),
            "currency_code": self.currency_code,
            "payment_intent_status": self.payment_intent_status,
            "payment_amount": format_exact_decimal(self.payment_amount),
            "source_payload_hash": self.source_payload_hash,
            "projected_at": _format_projected_at(self.projected_at),
        }


class PaymentIntentService:
    """Append-only payment-intent projector backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def project_payment_intent(
        self, tenant_reference: str, collection_case_id: UUID
    ) -> PaymentIntentResult:
        """Project one provider-neutral payment intent from a collection case.

        A replay of the same tenant, case, source-payload hash, and contract
        version returns the stored ``payment_intent_id``.  Another tenant
        cannot see or project that case.  The operator next binds a payment
        provider projection or cancels the intent.  This service never
        captures, settles, or posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(PaymentIntentRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(PaymentIntentRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)

        payment_amount = parse_payment_amount(collection_case.outstanding_amount)
        if payment_amount <= 0:
            return _rejected(PaymentIntentRejectionReasonCode.PAYMENT_AMOUNT_INVALID)

        source_payload_hash = compute_payment_intent_payload_hash(
            _canonical_case_snapshot(collection_case)
        )
        existing = self.ledger.find_payment_intent(
            tenant.tenant_account_id,
            collection_case.collection_case_id,
            source_payload_hash,
            PAYMENT_INTENT_CONTRACT_VERSION,
        )
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, PaymentIntentOutcomeCode.DUPLICATE_REPLAY
            )

        stored = self.ledger.insert_payment_intent(
            StoredPaymentIntent(
                payment_intent_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                collection_case_id=collection_case.collection_case_id,
                payment_intent_contract_version=PAYMENT_INTENT_CONTRACT_VERSION,
                currency_code=collection_case.currency_code,
                payment_intent_status=PAYMENT_INTENT_STATUS,
                payment_amount=payment_amount,
                source_payload_hash=source_payload_hash,
                projected_at=self._clock(),
            )
        )
        return _from_stored(stored, tenant.tenant_reference, PaymentIntentOutcomeCode.ACCEPTED)


def _canonical_case_snapshot(collection_case: StoredCollectionCase) -> dict[str, object]:
    """Return outstanding, currency, and status facts used for intent identity."""
    return {
        "collection_case_id": str(collection_case.collection_case_id),
        "outstanding_amount": format_exact_decimal(collection_case.outstanding_amount),
        "currency_code": collection_case.currency_code,
        "collection_case_status": collection_case.collection_case_status,
        "payment_intent_contract_version": PAYMENT_INTENT_CONTRACT_VERSION,
    }


def _rejected(reason_code: PaymentIntentRejectionReasonCode) -> PaymentIntentResult:
    """Build a rejected result without writing a payment intent."""
    return PaymentIntentResult(
        payment_intent_outcome_code=PaymentIntentOutcomeCode.REJECTED,
        payment_intent_contract_version=PAYMENT_INTENT_CONTRACT_VERSION,
        payment_intent_id=None,
        collection_case_id=None,
        tenant_reference=None,
        currency_code=None,
        payment_intent_status=None,
        payment_amount=None,
        source_payload_hash=None,
        projected_at=None,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredPaymentIntent,
    tenant_reference: str,
    outcome: PaymentIntentOutcomeCode,
) -> PaymentIntentResult:
    """Project a persisted intent into the buyer-facing result."""
    return PaymentIntentResult(
        payment_intent_outcome_code=outcome,
        payment_intent_contract_version=stored.payment_intent_contract_version,
        payment_intent_id=stored.payment_intent_id,
        collection_case_id=stored.collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        payment_intent_status=stored.payment_intent_status,
        payment_amount=stored.payment_amount,
        source_payload_hash=stored.source_payload_hash,
        projected_at=stored.projected_at,
        rejection_reason_code=None,
    )


def _format_projected_at(projected_at: datetime) -> str:
    """Render ``projected_at`` as a timezone-aware ISO 8601 instant."""
    return projected_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
