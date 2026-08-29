"""Record the controlled rating consumption of one applied late adjustment.

The existing :class:`UsageRatingService` rates usage snapshots against a
versioned rate card.  A late adjustment carries neither input, so this command
records its already-authoritative signed commercial delta as a separate rating
fact.  It never rewrites the original rating run or source adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    LateAdjustmentRatingOutcomeCode,
    LateAdjustmentRatingRejectionReasonCode,
    LateAdjustmentRatingTargetPeriodNotOpen,
    require_resolved,
)
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredLateAdjustmentRating,
    generate_record_id,
)


Clock = Callable[[], datetime]
LATE_ADJUSTMENT_RATING_CONTRACT_VERSION = 1
LATE_ADJUSTMENT_RATING_STATUS = "rated"
OPERATOR_ACTION_RECORD_INVOICE_ADJUSTMENT = "record_invoice_adjustment"
OPERATOR_ACTION_APPLY = "apply_late_adjustment"
OPERATOR_ACTION_WAIT = "wait"


@dataclass(frozen=True)
class LateAdjustmentRatingResult:
    """Buyer-facing result of rating one applied late adjustment."""

    late_adjustment_rating_outcome_code: LateAdjustmentRatingOutcomeCode
    late_adjustment_rating_contract_version: int
    late_adjustment_rating_id: UUID | None
    late_adjustment_application_id: UUID | None
    late_adjustment_id: UUID | None
    tenant_reference: str | None
    target_period_id: UUID | None
    adjustment_amount: Decimal | None
    currency_code: str | None
    rated_by: str | None
    authorization_reference: str | None
    rated_at: datetime | None
    late_adjustment_rating_status: str | None
    next_operator_action: str
    rejection_reason_code: LateAdjustmentRatingRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed accepted, replay, or rejected result object."""
        outcome = self.late_adjustment_rating_outcome_code.value
        if outcome == LateAdjustmentRatingOutcomeCode.REJECTED:
            return {
                "late_adjustment_rating_contract_version": (
                    self.late_adjustment_rating_contract_version
                ),
                "late_adjustment_rating_outcome_code": outcome,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else LateAdjustmentRatingRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND.value
                ),
                "next_operator_action": self.next_operator_action,
            }
        required = (
            self.late_adjustment_rating_id,
            self.late_adjustment_application_id,
            self.late_adjustment_id,
            self.tenant_reference,
            self.target_period_id,
            self.adjustment_amount,
            self.currency_code,
            self.rated_by,
            self.authorization_reference,
            self.rated_at,
            self.late_adjustment_rating_status,
        )
        if any(value is None for value in required):
            raise ValueError("accepted rating must include its immutable evidence")
        return {
            "late_adjustment_rating_contract_version": (
                self.late_adjustment_rating_contract_version
            ),
            "late_adjustment_rating_outcome_code": outcome,
            "late_adjustment_rating_id": str(self.late_adjustment_rating_id),
            "late_adjustment_application_id": str(self.late_adjustment_application_id),
            "late_adjustment_id": str(self.late_adjustment_id),
            "tenant_reference": self.tenant_reference,
            "target_period_id": str(self.target_period_id),
            "adjustment_amount": format(self.adjustment_amount, "f"),
            "currency_code": self.currency_code,
            "rated_by": self.rated_by,
            "authorization_reference": self.authorization_reference,
            "rated_at": _format_rated_at(self.rated_at),
            "late_adjustment_rating_status": self.late_adjustment_rating_status,
            "next_operator_action": self.next_operator_action,
        }


class LateAdjustmentRatingService:
    """Append one tenant-safe rating fact for applied adjustment evidence."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def rate_late_adjustment(
        self,
        tenant_reference: str,
        late_adjustment_id: UUID,
        *,
        rated_by: object,
        authorization_reference: object,
    ) -> LateAdjustmentRatingResult:
        """Record one rating fact or return its immutable replay."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.TENANT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        tenant = require_resolved(tenant, "tenant")
        if not isinstance(late_adjustment_id, UUID):
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        adjustment = self.ledger.get_late_adjustment(
            tenant.tenant_reference, late_adjustment_id
        )
        if adjustment is None:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        rated_by_text = _audit_reference(rated_by)
        if rated_by_text is None:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.ACTOR_REFERENCE_INVALID,
                OPERATOR_ACTION_WAIT,
            )
        authorization_text = _audit_reference(authorization_reference)
        if authorization_text is None:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.AUTHORIZATION_REFERENCE_INVALID,
                OPERATOR_ACTION_WAIT,
            )
        application = self.ledger.find_late_adjustment_application(
            tenant.tenant_account_id, adjustment.late_adjustment_id
        )
        if application is None:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.LATE_ADJUSTMENT_APPLICATION_NOT_FOUND,
                OPERATOR_ACTION_APPLY,
            )
        existing = self.ledger.find_late_adjustment_rating(
            tenant.tenant_account_id, adjustment.late_adjustment_id
        )
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                LateAdjustmentRatingOutcomeCode.DUPLICATE_REPLAY,
            )
        candidate = StoredLateAdjustmentRating(
            late_adjustment_rating_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            late_adjustment_application_id=application.late_adjustment_application_id,
            late_adjustment_id=adjustment.late_adjustment_id,
            target_period_id=adjustment.target_period_id,
            adjustment_amount=adjustment.adjustment_amount,
            currency_code=adjustment.currency_code,
            rated_by=rated_by_text,
            authorization_reference=authorization_text,
            rated_at=self._clock(),
            late_adjustment_rating_contract_version=(
                LATE_ADJUSTMENT_RATING_CONTRACT_VERSION
            ),
            late_adjustment_rating_status=LATE_ADJUSTMENT_RATING_STATUS,
        )
        try:
            stored = self.ledger.insert_late_adjustment_rating(candidate)
        except LateAdjustmentRatingTargetPeriodNotOpen:
            return _rejected(
                LateAdjustmentRatingRejectionReasonCode.TARGET_PERIOD_NOT_OPEN,
                OPERATOR_ACTION_WAIT,
            )
        outcome = (
            LateAdjustmentRatingOutcomeCode.ACCEPTED
            if stored.late_adjustment_rating_id == candidate.late_adjustment_rating_id
            else LateAdjustmentRatingOutcomeCode.DUPLICATE_REPLAY
        )
        return _from_stored(stored, tenant.tenant_reference, outcome)


def _audit_reference(value: object) -> str | None:
    """Normalize a required non-empty audit reference."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _rejected(
    reason_code: LateAdjustmentRatingRejectionReasonCode,
    next_operator_action: str,
) -> LateAdjustmentRatingResult:
    """Build a sparse rejected result without writing a fact."""
    return LateAdjustmentRatingResult(
        late_adjustment_rating_outcome_code=LateAdjustmentRatingOutcomeCode.REJECTED,
        late_adjustment_rating_contract_version=LATE_ADJUSTMENT_RATING_CONTRACT_VERSION,
        late_adjustment_rating_id=None,
        late_adjustment_application_id=None,
        late_adjustment_id=None,
        tenant_reference=None,
        target_period_id=None,
        adjustment_amount=None,
        currency_code=None,
        rated_by=None,
        authorization_reference=None,
        rated_at=None,
        late_adjustment_rating_status=None,
        next_operator_action=next_operator_action,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredLateAdjustmentRating,
    tenant_reference: str,
    outcome: LateAdjustmentRatingOutcomeCode,
) -> LateAdjustmentRatingResult:
    """Project one persisted rating fact into its stable contract."""
    return LateAdjustmentRatingResult(
        late_adjustment_rating_outcome_code=outcome,
        late_adjustment_rating_contract_version=(
            stored.late_adjustment_rating_contract_version
        ),
        late_adjustment_rating_id=stored.late_adjustment_rating_id,
        late_adjustment_application_id=stored.late_adjustment_application_id,
        late_adjustment_id=stored.late_adjustment_id,
        tenant_reference=tenant_reference,
        target_period_id=stored.target_period_id,
        adjustment_amount=stored.adjustment_amount,
        currency_code=stored.currency_code,
        rated_by=stored.rated_by,
        authorization_reference=stored.authorization_reference,
        rated_at=stored.rated_at,
        late_adjustment_rating_status=stored.late_adjustment_rating_status,
        next_operator_action=OPERATOR_ACTION_RECORD_INVOICE_ADJUSTMENT,
        rejection_reason_code=None,
    )


def _format_rated_at(rated_at: datetime) -> str:
    """Render a rating instant as UTC ISO 8601."""
    return rated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
