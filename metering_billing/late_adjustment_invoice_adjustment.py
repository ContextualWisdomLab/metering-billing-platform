"""Compose a rated late-adjustment fact into an invoice draft.

The composition is a separate immutable invoice-intent fact.  It never edits
the original draft or an issued invoice snapshot; a later issuer/exporter can
consume this explicit signed line without losing the source rating evidence.
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
    LateAdjustmentInvoiceAdjustmentOutcomeCode,
    LateAdjustmentInvoiceAdjustmentRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import issued_invoice_amount_exceeds_storage_precision
from metering_billing.usage_ledger import (
    LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION,
    MemoryUsageLedger,
    StoredLateAdjustmentInvoiceAdjustment,
    _validate_audit_timestamp,
    generate_record_id,
)


Clock = Callable[[], datetime]
LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_STATUS = "recorded"
OPERATOR_ACTION_ISSUE_INVOICE = "issue_invoice"
OPERATOR_ACTION_APPLY = "apply_late_adjustment"
OPERATOR_ACTION_RATE = "rate_late_adjustment"
OPERATOR_ACTION_WAIT = "wait"


@dataclass(frozen=True)
class LateAdjustmentInvoiceAdjustmentResult:
    """Buyer-facing result of composing one rated adjustment into a draft."""

    late_adjustment_invoice_adjustment_outcome_code: (
        LateAdjustmentInvoiceAdjustmentOutcomeCode
    )
    late_adjustment_invoice_adjustment_contract_version: int
    late_adjustment_invoice_adjustment_id: UUID | None
    late_adjustment_rating_id: UUID | None
    late_adjustment_application_id: UUID | None
    late_adjustment_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    billing_account_id: UUID | None
    billing_account_reference: str | None
    target_period_id: UUID | None
    adjustment_amount: Decimal | None
    currency_code: str | None
    recorded_by: str | None
    authorization_reference: str | None
    recorded_at: datetime | None
    source_payload_hash: str | None
    late_adjustment_invoice_adjustment_status: str | None
    next_operator_action: str
    rejection_reason_code: LateAdjustmentInvoiceAdjustmentRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the accepted, replay, or sparse rejected contract."""
        outcome = self.late_adjustment_invoice_adjustment_outcome_code.value
        if outcome == LateAdjustmentInvoiceAdjustmentOutcomeCode.REJECTED:
            return {
                "late_adjustment_invoice_adjustment_contract_version": (
                    self.late_adjustment_invoice_adjustment_contract_version
                ),
                "late_adjustment_invoice_adjustment_outcome_code": outcome,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND.value
                ),
                "next_operator_action": self.next_operator_action,
            }
        required = (
            self.late_adjustment_invoice_adjustment_id,
            self.late_adjustment_rating_id,
            self.late_adjustment_application_id,
            self.late_adjustment_id,
            self.invoice_draft_id,
            self.tenant_reference,
            self.billing_account_id,
            self.billing_account_reference,
            self.target_period_id,
            self.adjustment_amount,
            self.currency_code,
            self.recorded_by,
            self.authorization_reference,
            self.recorded_at,
            self.source_payload_hash,
            self.late_adjustment_invoice_adjustment_status,
        )
        if any(value is None for value in required):
            raise ValueError("accepted composition must include immutable evidence")
        return {
            "late_adjustment_invoice_adjustment_contract_version": (
                self.late_adjustment_invoice_adjustment_contract_version
            ),
            "late_adjustment_invoice_adjustment_outcome_code": outcome,
            "late_adjustment_invoice_adjustment_id": str(
                self.late_adjustment_invoice_adjustment_id
            ),
            "late_adjustment_rating_id": str(self.late_adjustment_rating_id),
            "late_adjustment_application_id": str(self.late_adjustment_application_id),
            "late_adjustment_id": str(self.late_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "billing_account_reference": self.billing_account_reference,
            "target_period_id": str(self.target_period_id),
            "adjustment_amount": format(self.adjustment_amount, "f"),
            "currency_code": self.currency_code,
            "recorded_by": self.recorded_by,
            "authorization_reference": self.authorization_reference,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "source_payload_hash": self.source_payload_hash,
            "late_adjustment_invoice_adjustment_status": (
                self.late_adjustment_invoice_adjustment_status
            ),
            "next_operator_action": self.next_operator_action,
        }


class LateAdjustmentInvoiceAdjustmentService:
    """Append one tenant-safe invoice-intent composition for a rated fact."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def record_invoice_adjustment(
        self,
        tenant_reference: str,
        late_adjustment_id: UUID,
        invoice_draft_id: UUID,
        *,
        recorded_by: object,
        authorization_reference: object,
    ) -> LateAdjustmentInvoiceAdjustmentResult:
        """Attach one rated signed delta to an unissued same-currency draft."""
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._record_invoice_adjustment(
                tenant_reference,
                late_adjustment_id,
                invoice_draft_id,
                recorded_by=recorded_by,
                authorization_reference=authorization_reference,
            )
        with transaction():
            return self._record_invoice_adjustment(
                tenant_reference,
                late_adjustment_id,
                invoice_draft_id,
                recorded_by=recorded_by,
                authorization_reference=authorization_reference,
            )

    def _record_invoice_adjustment(
        self,
        tenant_reference: str,
        late_adjustment_id: UUID,
        invoice_draft_id: UUID,
        *,
        recorded_by: object,
        authorization_reference: object,
    ) -> LateAdjustmentInvoiceAdjustmentResult:
        """Record the composition inside the caller's transaction boundary."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.TENANT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        tenant = require_resolved(tenant, "tenant")
        if not isinstance(late_adjustment_id, UUID):
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        adjustment = self.ledger.get_late_adjustment(
            tenant.tenant_reference, late_adjustment_id
        )
        if adjustment is None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.LATE_ADJUSTMENT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        recorded_by_text = _audit_reference(recorded_by)
        if recorded_by_text is None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.ACTOR_REFERENCE_INVALID,
                OPERATOR_ACTION_WAIT,
            )
        authorization_text = _audit_reference(authorization_reference)
        if authorization_text is None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.AUTHORIZATION_REFERENCE_INVALID,
                OPERATOR_ACTION_WAIT,
            )
        if not isinstance(invoice_draft_id, UUID):
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        rating = self.ledger.find_late_adjustment_rating(
            tenant.tenant_account_id, adjustment.late_adjustment_id
        )
        if rating is None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.LATE_ADJUSTMENT_RATING_NOT_FOUND,
                OPERATOR_ACTION_RATE,
            )
        existing = self.ledger.find_late_adjustment_invoice_adjustment(
            tenant.tenant_account_id, rating.late_adjustment_rating_id
        )
        if existing is not None:
            if existing.invoice_draft_id != invoice_draft_id:
                return _rejected(
                    LateAdjustmentInvoiceAdjustmentRejectionReasonCode.IDENTITY_CONFLICT,
                    OPERATOR_ACTION_WAIT,
                )
            return _from_stored(
                existing,
                tenant.tenant_reference,
                LateAdjustmentInvoiceAdjustmentOutcomeCode.DUPLICATE_REPLAY,
            )
        draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if draft is None or draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        locked_draft = getattr(self.ledger, "lock_invoice_draft", None)
        if locked_draft is not None:
            draft = locked_draft(tenant.tenant_account_id, draft.invoice_draft_id)
            if draft is None:
                return _rejected(
                    LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
                    OPERATOR_ACTION_WAIT,
                )
        if draft.currency_code != rating.currency_code:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.CURRENCY_MISMATCH,
                OPERATOR_ACTION_WAIT,
            )
        if self.ledger.find_issued_invoice(
            tenant.tenant_account_id, draft.invoice_draft_id
        ) is not None:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_ALREADY_ISSUED,
                OPERATOR_ACTION_WAIT,
            )
        if _has_downstream_records(self.ledger, tenant.tenant_account_id, draft.invoice_draft_id):
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_HAS_DOWNSTREAM_RECORDS,
                OPERATOR_ACTION_WAIT,
            )
        billing_accounts = {
            (line.billing_account_id, line.billing_account_reference)
            for line in draft.invoice_draft_lines
        }
        if not billing_accounts:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_BILLING_ACCOUNT_NOT_FOUND,
                OPERATOR_ACTION_WAIT,
            )
        if len(billing_accounts) != 1:
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_DRAFT_BILLING_ACCOUNT_AMBIGUOUS,
                OPERATOR_ACTION_WAIT,
            )
        billing_account_id, billing_account_reference = next(iter(billing_accounts))
        if issued_invoice_amount_exceeds_storage_precision(rating.adjustment_amount):
            return _rejected(
                LateAdjustmentInvoiceAdjustmentRejectionReasonCode.ADJUSTMENT_AMOUNT_NOT_REPRESENTABLE,
                OPERATOR_ACTION_WAIT,
            )
        source_payload_hash = _payload_hash(
            {
                "invoice_draft_id": str(draft.invoice_draft_id),
                "late_adjustment_application_id": str(
                    rating.late_adjustment_application_id
                ),
                "late_adjustment_id": str(rating.late_adjustment_id),
                "late_adjustment_rating_id": str(rating.late_adjustment_rating_id),
                "target_period_id": str(rating.target_period_id),
                "adjustment_amount": format(rating.adjustment_amount, "f"),
                "currency_code": rating.currency_code,
                "billing_account_id": str(billing_account_id),
                "billing_account_reference": billing_account_reference,
                "late_adjustment_invoice_adjustment_contract_version": (
                    LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION
                ),
            }
        )
        candidate = StoredLateAdjustmentInvoiceAdjustment(
            late_adjustment_invoice_adjustment_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            billing_account_id=billing_account_id,
            billing_account_reference=billing_account_reference,
            late_adjustment_rating_id=rating.late_adjustment_rating_id,
            late_adjustment_application_id=rating.late_adjustment_application_id,
            late_adjustment_id=rating.late_adjustment_id,
            invoice_draft_id=draft.invoice_draft_id,
            target_period_id=rating.target_period_id,
            adjustment_amount=rating.adjustment_amount,
            currency_code=rating.currency_code,
            recorded_by=recorded_by_text,
            authorization_reference=authorization_text,
            recorded_at=_validate_audit_timestamp(self._clock(), "recorded_at"),
            source_payload_hash=source_payload_hash,
            late_adjustment_invoice_adjustment_contract_version=(
                LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION
            ),
            late_adjustment_invoice_adjustment_status=(
                LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_STATUS
            ),
        )
        try:
            stored = self.ledger.insert_late_adjustment_invoice_adjustment(candidate)
        except ValueError as error:
            if str(error) == "invoice draft already has an issued invoice":
                return _rejected(
                    LateAdjustmentInvoiceAdjustmentRejectionReasonCode.INVOICE_ALREADY_ISSUED,
                    OPERATOR_ACTION_WAIT,
                )
            if str(error) == "adjustment_amount exceeds numeric(38,12) precision":
                return _rejected(
                    LateAdjustmentInvoiceAdjustmentRejectionReasonCode.ADJUSTMENT_AMOUNT_NOT_REPRESENTABLE,
                    OPERATOR_ACTION_WAIT,
                )
            if str(error) == "late adjustment invoice adjustment identity conflicts with an existing row":
                return _rejected(
                    LateAdjustmentInvoiceAdjustmentRejectionReasonCode.IDENTITY_CONFLICT,
                    OPERATOR_ACTION_WAIT,
                )
            raise
        outcome = (
            LateAdjustmentInvoiceAdjustmentOutcomeCode.ACCEPTED
            if stored.late_adjustment_invoice_adjustment_id
            == candidate.late_adjustment_invoice_adjustment_id
            else LateAdjustmentInvoiceAdjustmentOutcomeCode.DUPLICATE_REPLAY
        )
        return _from_stored(stored, tenant.tenant_reference, outcome)


def _audit_reference(value: object) -> str | None:
    """Normalize a required non-empty audit reference."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _has_downstream_records(ledger: MemoryUsageLedger, tenant_account_id: UUID, invoice_draft_id: UUID) -> bool:
    """Reject composition once a downstream fact has captured the old draft."""
    if ledger.find_collection_case(tenant_account_id, invoice_draft_id) is not None:
        return True
    if ledger.find_journal_proposal_for_invoice_draft(tenant_account_id, invoice_draft_id) is not None:
        return True
    if ledger.find_tax_assessment_for_draft(tenant_account_id, invoice_draft_id) is not None:
        return True
    return any(
        credit.invoice_draft_id == invoice_draft_id
        for credit in ledger.list_credit_adjustments(tenant_account_id)
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable hash for the composed invoice-intent identity."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejected(
    reason_code: LateAdjustmentInvoiceAdjustmentRejectionReasonCode,
    next_operator_action: str,
) -> LateAdjustmentInvoiceAdjustmentResult:
    """Build a sparse rejected result without writing a fact."""
    return LateAdjustmentInvoiceAdjustmentResult(
        late_adjustment_invoice_adjustment_outcome_code=(
            LateAdjustmentInvoiceAdjustmentOutcomeCode.REJECTED
        ),
        late_adjustment_invoice_adjustment_contract_version=(
            LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION
        ),
        late_adjustment_invoice_adjustment_id=None,
        late_adjustment_rating_id=None,
        late_adjustment_application_id=None,
        late_adjustment_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        billing_account_id=None,
        billing_account_reference=None,
        target_period_id=None,
        adjustment_amount=None,
        currency_code=None,
        recorded_by=None,
        authorization_reference=None,
        recorded_at=None,
        source_payload_hash=None,
        late_adjustment_invoice_adjustment_status=None,
        next_operator_action=next_operator_action,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredLateAdjustmentInvoiceAdjustment,
    tenant_reference: str,
    outcome: LateAdjustmentInvoiceAdjustmentOutcomeCode,
) -> LateAdjustmentInvoiceAdjustmentResult:
    """Project one stored composition into its stable contract."""
    return LateAdjustmentInvoiceAdjustmentResult(
        late_adjustment_invoice_adjustment_outcome_code=outcome,
        late_adjustment_invoice_adjustment_contract_version=(
            stored.late_adjustment_invoice_adjustment_contract_version
        ),
        late_adjustment_invoice_adjustment_id=(
            stored.late_adjustment_invoice_adjustment_id
        ),
        late_adjustment_rating_id=stored.late_adjustment_rating_id,
        late_adjustment_application_id=stored.late_adjustment_application_id,
        late_adjustment_id=stored.late_adjustment_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        billing_account_id=stored.billing_account_id,
        billing_account_reference=stored.billing_account_reference,
        target_period_id=stored.target_period_id,
        adjustment_amount=stored.adjustment_amount,
        currency_code=stored.currency_code,
        recorded_by=stored.recorded_by,
        authorization_reference=stored.authorization_reference,
        recorded_at=stored.recorded_at,
        source_payload_hash=stored.source_payload_hash,
        late_adjustment_invoice_adjustment_status=(
            stored.late_adjustment_invoice_adjustment_status
        ),
        next_operator_action=OPERATOR_ACTION_ISSUE_INVOICE,
        rejection_reason_code=None,
    )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Render a timezone-aware instant as UTC ISO 8601."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
