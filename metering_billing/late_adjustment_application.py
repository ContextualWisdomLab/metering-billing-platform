"""Record the controlled downstream application of one late adjustment.

This slice acknowledges consumption of immutable late-adjustment evidence. It
does not rewrite a period, usage fact, rating run, tax assessment, journal, or
provider state; re-rating remains a separate command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    LateAdjustmentApplicationOutcomeCode,
    LateAdjustmentApplicationRejectionReasonCode,
    require_resolved,
)
from metering_billing.period_close import BillingPeriodStatus
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredLateAdjustmentApplication,
    _validate_audit_timestamp,
    generate_record_id,
)


Clock = Callable[[], datetime]
LATE_ADJUSTMENT_APPLICATION_CONTRACT_VERSION = 1
LATE_ADJUSTMENT_APPLICATION_STATUS = "applied"
OPERATOR_ACTION_RATE = "rate_late_adjustment"
OPERATOR_ACTION_WAIT = "wait"


@dataclass(frozen=True)
class LateAdjustmentApplicationResult:
    """Buyer-facing result of acknowledging one late-adjustment fact."""

    late_adjustment_application_outcome_code: LateAdjustmentApplicationOutcomeCode
    late_adjustment_application_contract_version: int
    late_adjustment_application_id: UUID | None
    late_adjustment_id: UUID | None
    tenant_reference: str | None
    target_period_id: UUID | None
    adjustment_amount: Decimal | None
    currency_code: str | None
    applied_by: str | None
    authorization_reference: str | None
    applied_at: datetime | None
    late_adjustment_application_status: str | None
    next_operator_action: str
    rejection_reason_code: LateAdjustmentApplicationRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed accepted, replay, or rejected result object."""
        outcome = self.late_adjustment_application_outcome_code.value
        if outcome == LateAdjustmentApplicationOutcomeCode.REJECTED:
            return {
                "late_adjustment_application_contract_version": (
                    self.late_adjustment_application_contract_version
                ),
                "late_adjustment_application_outcome_code": outcome,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else LateAdjustmentApplicationRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND.value
                ),
                "next_operator_action": self.next_operator_action,
            }
        if self.late_adjustment_application_id is None:
            raise ValueError("accepted application must include an identifier")
        if self.late_adjustment_id is None or self.tenant_reference is None:
            raise ValueError("accepted application must include source identity")
        if self.target_period_id is None or self.adjustment_amount is None:
            raise ValueError("accepted application must include exact target amount")
        if self.currency_code is None or self.applied_by is None:
            raise ValueError("accepted application must include audit references")
        if self.authorization_reference is None or self.applied_at is None:
            raise ValueError("accepted application must include authorization and time")
        if self.late_adjustment_application_status is None:
            raise ValueError("accepted application must include status")
        return {
            "late_adjustment_application_contract_version": (
                self.late_adjustment_application_contract_version
            ),
            "late_adjustment_application_outcome_code": outcome,
            "late_adjustment_application_id": str(self.late_adjustment_application_id),
            "late_adjustment_id": str(self.late_adjustment_id),
            "tenant_reference": self.tenant_reference,
            "target_period_id": str(self.target_period_id),
            "adjustment_amount": format(self.adjustment_amount, "f"),
            "currency_code": self.currency_code,
            "applied_by": self.applied_by,
            "authorization_reference": self.authorization_reference,
            "applied_at": _format_applied_at(self.applied_at),
            "late_adjustment_application_status": self.late_adjustment_application_status,
            "next_operator_action": self.next_operator_action,
        }


class LateAdjustmentApplicationService:
    """Append one tenant-safe application fact for recorded evidence."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def apply_late_adjustment(
        self,
        tenant_reference: str,
        late_adjustment_id: UUID,
        *,
        applied_by: object,
        authorization_reference: object,
    ) -> LateAdjustmentApplicationResult:
        """Record one application or return its immutable replay."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.TENANT_NOT_FOUND
            )
        tenant = require_resolved(tenant, "tenant")
        if not isinstance(late_adjustment_id, UUID):
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND
            )
        adjustment = self.ledger.get_late_adjustment(
            tenant.tenant_reference, late_adjustment_id
        )
        if adjustment is None:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND
            )
        applied_by_text = _audit_reference(applied_by)
        if applied_by_text is None:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.ACTOR_REFERENCE_INVALID
            )
        authorization_text = _audit_reference(authorization_reference)
        if authorization_text is None:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.AUTHORIZATION_REFERENCE_INVALID
            )
        existing = self.ledger.find_late_adjustment_application(
            tenant.tenant_account_id, adjustment.late_adjustment_id
        )
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                LateAdjustmentApplicationOutcomeCode.DUPLICATE_REPLAY,
            )
        target = self.ledger.get_billing_period(
            tenant.tenant_reference, adjustment.target_period_id
        )
        if target is None:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.TARGET_PERIOD_NOT_FOUND
            )
        if target.status != BillingPeriodStatus.OPEN:
            return _rejected(
                LateAdjustmentApplicationRejectionReasonCode.TARGET_PERIOD_NOT_OPEN
            )
        candidate = StoredLateAdjustmentApplication(
            late_adjustment_application_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            late_adjustment_id=adjustment.late_adjustment_id,
            target_period_id=adjustment.target_period_id,
            adjustment_amount=adjustment.adjustment_amount,
            currency_code=adjustment.currency_code,
            applied_by=applied_by_text,
            authorization_reference=authorization_text,
            applied_at=_validate_audit_timestamp(self._clock(), "applied_at"),
            late_adjustment_application_contract_version=(
                LATE_ADJUSTMENT_APPLICATION_CONTRACT_VERSION
            ),
            late_adjustment_application_status=LATE_ADJUSTMENT_APPLICATION_STATUS,
        )
        try:
            stored = self.ledger.insert_late_adjustment_application(candidate)
        except Exception as error:
            rejection_reason = _target_period_rejection_reason(error)
            if rejection_reason is None:
                raise
            return _rejected(rejection_reason)
        outcome = (
            LateAdjustmentApplicationOutcomeCode.ACCEPTED
            if stored.late_adjustment_application_id
            == candidate.late_adjustment_application_id
            else LateAdjustmentApplicationOutcomeCode.DUPLICATE_REPLAY
        )
        return _from_stored(stored, tenant.tenant_reference, outcome)


def _audit_reference(value: object) -> str | None:
    """Normalize a required non-empty audit reference."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _target_period_rejection_reason(
    error: Exception,
) -> LateAdjustmentApplicationRejectionReasonCode | None:
    """Map only the PostgreSQL trigger's target-lifecycle failures."""
    message = getattr(getattr(error, "diag", None), "message_primary", None)
    if message is None and isinstance(error, ValueError):
        message = str(error)
    return {
        "late adjustment application target period is missing": (
            LateAdjustmentApplicationRejectionReasonCode.TARGET_PERIOD_NOT_FOUND
        ),
        "late adjustment application target period must be open": (
            LateAdjustmentApplicationRejectionReasonCode.TARGET_PERIOD_NOT_OPEN
        ),
    }.get(message)


def _rejected(
    reason_code: LateAdjustmentApplicationRejectionReasonCode,
) -> LateAdjustmentApplicationResult:
    """Build a sparse rejected result without writing a fact."""
    return LateAdjustmentApplicationResult(
        late_adjustment_application_outcome_code=LateAdjustmentApplicationOutcomeCode.REJECTED,
        late_adjustment_application_contract_version=(
            LATE_ADJUSTMENT_APPLICATION_CONTRACT_VERSION
        ),
        late_adjustment_application_id=None,
        late_adjustment_id=None,
        tenant_reference=None,
        target_period_id=None,
        adjustment_amount=None,
        currency_code=None,
        applied_by=None,
        authorization_reference=None,
        applied_at=None,
        late_adjustment_application_status=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredLateAdjustmentApplication,
    tenant_reference: str,
    outcome: LateAdjustmentApplicationOutcomeCode,
) -> LateAdjustmentApplicationResult:
    """Project one persisted application into its stable contract."""
    return LateAdjustmentApplicationResult(
        late_adjustment_application_outcome_code=outcome,
        late_adjustment_application_contract_version=(
            stored.late_adjustment_application_contract_version
        ),
        late_adjustment_application_id=stored.late_adjustment_application_id,
        late_adjustment_id=stored.late_adjustment_id,
        tenant_reference=tenant_reference,
        target_period_id=stored.target_period_id,
        adjustment_amount=stored.adjustment_amount,
        currency_code=stored.currency_code,
        applied_by=stored.applied_by,
        authorization_reference=stored.authorization_reference,
        applied_at=stored.applied_at,
        late_adjustment_application_status=stored.late_adjustment_application_status,
        next_operator_action=OPERATOR_ACTION_RATE,
        rejection_reason_code=None,
    )


def _format_applied_at(applied_at: datetime) -> str:
    """Render an application instant as UTC ISO 8601."""
    return applied_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
