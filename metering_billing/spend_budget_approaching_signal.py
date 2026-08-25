"""Observe a published spend budget and enqueue spend_budget.approaching once.

The service is the buyer-facing approaching-signal write:

1. Reuse ``SpendBudgetEvaluationPresentmentService`` for the same remaining
   and over math as ``GET /v1/spend-budgets/{id}/evaluation``.
2. Enqueue one existing ``spend_budget.approaching`` outbox event only when
   ``utilization_status`` is ``at`` the documented ``budget_amount``
   threshold.
3. Replay of the same ``spend_budget_id`` source does not grow the outbox.

under and over write zero approaching-signal rows.  Rejected observe
writes zero rows.  The write does not persist an evaluation snapshot,
hard-stop rating, compose a journal, or call AIS (IFRS Foundation, 2024).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    SpendBudgetApproachingSignalOutcomeCode,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.spend_budget import (
    NEXT_OPERATOR_ACTION,
    SPEND_BUDGET_CONTRACT_VERSION,
    SPEND_BUDGET_STATUS,
)
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentResult,
    SpendBudgetEvaluationPresentmentService,
    UTILIZATION_AT,
    UTILIZATION_OVER,
    UTILIZATION_UNDER,
)
from metering_billing.usage_ledger import MemoryUsageLedger
from metering_billing.webhook_outbox import (
    EVENT_TYPE_SPEND_BUDGET_APPROACHING,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class SpendBudgetApproachingSignalResult:
    """Buyer-facing result of observing one published spend budget for approaching."""

    spend_budget_approaching_signal_outcome_code: SpendBudgetApproachingSignalOutcomeCode
    spend_budget_approaching_signal_contract_version: int
    spend_budget_id: UUID | None
    tenant_reference: str | None
    billing_account_id: UUID | None
    currency_code: str | None
    budget_amount: Decimal | None
    remaining_amount: Decimal | None
    utilization_status: str | None
    window_started_at: datetime | None
    window_ended_at: datetime | None
    spend_budget_status: str | None
    source_payload_hash: str | None
    spend_budget_contract_version: int
    next_operator_action: str
    rejection_reason_code: SpendBudgetRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the approaching-signal write, or a sparse rejected operational result."""
        outcome = self.spend_budget_approaching_signal_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, SpendBudgetApproachingSignalOutcomeCode)
            else str(outcome)
        )
        if outcome_text == SpendBudgetApproachingSignalOutcomeCode.REJECTED:
            return {
                "spend_budget_approaching_signal_contract_version": (
                    self.spend_budget_approaching_signal_contract_version
                ),
                "spend_budget_approaching_signal_outcome_code": outcome_text,
                "rejection_reason_code": self.rejection_reason_text(),
            }
        if (
            outcome_text != SpendBudgetApproachingSignalOutcomeCode.ACCEPTED
            and outcome_text != SpendBudgetApproachingSignalOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(
                f"unsupported spend budget approaching signal outcome: {outcome_text}"
            )
        if (
            self.spend_budget_id is None
            or self.tenant_reference is None
            or self.billing_account_id is None
            or self.currency_code is None
            or self.budget_amount is None
            or self.remaining_amount is None
            or self.window_started_at is None
            or self.window_ended_at is None
        ):
            raise ValueError("accepted approaching signals must include identity and amount")
        return {
            "spend_budget_approaching_signal_contract_version": (
                self.spend_budget_approaching_signal_contract_version
            ),
            "spend_budget_approaching_signal_outcome_code": outcome_text,
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "remaining_amount": format_exact_decimal(self.remaining_amount),
            "utilization_status": self.utilization_status,
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": SPEND_BUDGET_STATUS,
            "source_payload_hash": self.source_payload_hash,
            "spend_budget_contract_version": self.spend_budget_contract_version,
            "next_operator_action": self.next_operator_action,
        }

    def rejection_reason_text(self) -> str:
        """Return the serialized rejection reason used by HTTP status and body.

        A missing reason fail-closes as ``spend_budget_not_found`` so status
        and contract stay one vocabulary.  The service always supplies a
        reason; this default is the hollow-result guard.
        """
        if self.rejection_reason_code is not None:
            return self.rejection_reason_code.value
        return SpendBudgetRejectionReasonCode.SPEND_BUDGET_NOT_FOUND.value

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``spend_budget.approaching`` facts for the #24 envelope.

        The payload is a reference plus hash and exact remaining amount.
        Over, rated lines, PII, PAN, secrets, raw documents, and statutory
        identifiers are omitted so currencies stay unmixed.
        """
        if self.spend_budget_id is None or self.billing_account_id is None:
            raise ValueError("rejected spend budget approaching signal has no webhook event data")
        if self.utilization_status != UTILIZATION_AT:
            raise ValueError("only at observations have webhook event data")
        if self.remaining_amount is None:
            raise ValueError("accepted approaching signals must include remaining_amount")
        return {
            "spend_budget_id": str(self.spend_budget_id),
            "billing_account_id": str(self.billing_account_id),
            "source_payload_hash": self.source_payload_hash,
            "spend_budget_contract_version": self.spend_budget_contract_version,
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "remaining_amount": format_exact_decimal(self.remaining_amount),
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": SPEND_BUDGET_STATUS,
            "utilization_status": UTILIZATION_AT,
        }


class SpendBudgetApproachingSignalService:
    """Observe one published budget and enqueue ``spend_budget.approaching`` once."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._evaluations = SpendBudgetEvaluationPresentmentService(self.ledger)

    def observe_spend_budget_approaching(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetApproachingSignalResult:
        """Observe one published budget and enqueue approaching only when at.

        Same-tenant published budgets return ``accepted`` or
        ``duplicate_replay``.  under and over write zero approaching-signal
        rows.  Replay of the same ``spend_budget_id`` source does not
        enqueue a second row.  A crash after compute and before enqueue is
        healed by the next observe.
        """
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            return self._observe_spend_budget_approaching_in_boundary(
                tenant_reference, spend_budget_id
            )

    def _observe_spend_budget_approaching_in_boundary(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetApproachingSignalResult:
        """Evaluate and maybe enqueue inside the ledger boundary."""
        try:
            evaluation = self._evaluations.present_spend_budget_evaluation(
                tenant_reference, spend_budget_id
            )
        except SpendBudgetEvaluationPresentmentQueryError as error:
            return _rejected(_rejection_for(error.rejection_reason_code))
        utilization_status = evaluation.utilization_status
        if utilization_status == UTILIZATION_UNDER or utilization_status == UTILIZATION_OVER:
            return _from_evaluation(
                evaluation,
                SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
                _require_stored_budget_hash(self.ledger, evaluation.spend_budget_id),
            )
        if utilization_status != UTILIZATION_AT:
            raise ValueError(f"unsupported utilization status: {utilization_status}")
        budget = self.ledger.get_spend_budget(evaluation.spend_budget_id)
        if budget is None:
            raise ValueError("accepted approaching signals require a stored budget")
        existing = _existing_approaching_event(
            self.ledger, budget.tenant_account_id, evaluation.spend_budget_id
        )
        outcome = (
            SpendBudgetApproachingSignalOutcomeCode.DUPLICATE_REPLAY
            if existing is not None
            else SpendBudgetApproachingSignalOutcomeCode.ACCEPTED
        )
        result = _from_evaluation(evaluation, outcome, budget.source_payload_hash)
        if existing is None:
            _enqueue_spend_budget_approaching(self.ledger, tenant_reference, result, self._clock())
        return result


def _enqueue_spend_budget_approaching(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: SpendBudgetApproachingSignalResult,
    occurred_at: datetime,
) -> None:
    """Append one ``spend_budget.approaching`` outbox row for a stored at observation.

    Replay of the same tenant, event type, ``spend_budget_id``, and payload
    hash returns the stored row.  Callers that already found a source_id
    row skip this helper so a later distinct remaining amount cannot grow
    the outbox.
    """
    if result.spend_budget_id is None or result.remaining_amount is None:
        raise ValueError("accepted approaching signals must include identity and remaining_amount")
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_SPEND_BUDGET_APPROACHING,
        result.spend_budget_id,
        result.as_webhook_event_data(),
        occurred_at,
    )


def _existing_approaching_event(
    ledger: MemoryUsageLedger, tenant_account_id: UUID, spend_budget_id: UUID
):
    """Return the first ``spend_budget.approaching`` row for one source, if any."""
    for event in ledger.list_webhook_outbox_events_for_tenant(tenant_account_id):
        if (
            event.event_type_code == EVENT_TYPE_SPEND_BUDGET_APPROACHING
            and event.source_id == spend_budget_id
        ):
            return event
    return None


def _require_stored_budget_hash(ledger: MemoryUsageLedger, spend_budget_id: UUID) -> str:
    """Return the stored source-payload hash for one published budget."""
    budget = ledger.get_spend_budget(spend_budget_id)
    if budget is None:
        raise ValueError("accepted approaching signals require a stored budget")
    return budget.source_payload_hash


def _rejection_for(rejection_reason_code: str) -> SpendBudgetRejectionReasonCode:
    """Map an evaluation query failure onto the write rejection vocabulary."""
    try:
        return SpendBudgetRejectionReasonCode(rejection_reason_code)
    except ValueError:
        return SpendBudgetRejectionReasonCode.REQUEST_INVALID


def _rejected(reason: SpendBudgetRejectionReasonCode) -> SpendBudgetApproachingSignalResult:
    """Return a sparse rejected approaching-signal result."""
    return SpendBudgetApproachingSignalResult(
        spend_budget_approaching_signal_outcome_code=SpendBudgetApproachingSignalOutcomeCode.REJECTED,
        spend_budget_approaching_signal_contract_version=SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION,
        spend_budget_id=None,
        tenant_reference=None,
        billing_account_id=None,
        currency_code=None,
        budget_amount=None,
        remaining_amount=None,
        utilization_status=None,
        window_started_at=None,
        window_ended_at=None,
        spend_budget_status=None,
        source_payload_hash=None,
        spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=reason,
    )


def _from_evaluation(
    evaluation: SpendBudgetEvaluationPresentmentResult,
    outcome: SpendBudgetApproachingSignalOutcomeCode,
    source_payload_hash: str,
) -> SpendBudgetApproachingSignalResult:
    """Project one evaluation into the buyer-facing approaching-signal result."""
    return SpendBudgetApproachingSignalResult(
        spend_budget_approaching_signal_outcome_code=outcome,
        spend_budget_approaching_signal_contract_version=SPEND_BUDGET_APPROACHING_SIGNAL_CONTRACT_VERSION,
        spend_budget_id=evaluation.spend_budget_id,
        tenant_reference=evaluation.tenant_reference,
        billing_account_id=evaluation.billing_account_id,
        currency_code=evaluation.currency_code,
        budget_amount=evaluation.budget_amount,
        remaining_amount=evaluation.remaining_amount,
        utilization_status=evaluation.utilization_status,
        window_started_at=evaluation.window_started_at,
        window_ended_at=evaluation.window_ended_at,
        spend_budget_status=evaluation.spend_budget_status,
        source_payload_hash=source_payload_hash,
        spend_budget_contract_version=SPEND_BUDGET_CONTRACT_VERSION,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=None,
    )


def _format_instant(instant: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for one stored instant."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
