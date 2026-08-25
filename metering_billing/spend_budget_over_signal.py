"""Observe a published spend budget and enqueue spend_budget.over once.

The service is the buyer-facing over-signal write:

1. Reuse ``SpendBudgetEvaluationPresentmentService`` for the same remaining
   and over math as ``GET /v1/spend-budgets/{id}/evaluation``.
2. Enqueue one existing ``spend_budget.over`` outbox event only when
   ``utilization_status`` is ``over``.
3. Replay of the same ``spend_budget_id`` source does not grow the outbox.

under and at write zero over-signal rows.  Rejected observe writes zero
rows.  The write does not persist an evaluation snapshot, hard-stop
rating, compose a journal, or call AIS (IFRS Foundation, 2024).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetOverSignalOutcomeCode,
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
from metering_billing.webhook_outbox import EVENT_TYPE_SPEND_BUDGET_OVER, enqueue_accepted_fact


Clock = Callable[[], datetime]
SPEND_BUDGET_OVER_SIGNAL_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class SpendBudgetOverSignalResult:
    """Buyer-facing result of observing one published spend budget for over."""

    spend_budget_over_signal_outcome_code: SpendBudgetOverSignalOutcomeCode
    spend_budget_over_signal_contract_version: int
    spend_budget_id: UUID | None
    tenant_reference: str | None
    billing_account_id: UUID | None
    currency_code: str | None
    budget_amount: Decimal | None
    over_amount: Decimal | None
    utilization_status: str | None
    window_started_at: datetime | None
    window_ended_at: datetime | None
    spend_budget_status: str | None
    source_payload_hash: str | None
    spend_budget_contract_version: int
    next_operator_action: str
    rejection_reason_code: SpendBudgetRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the over-signal write, or a sparse rejected operational result."""
        outcome = self.spend_budget_over_signal_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, SpendBudgetOverSignalOutcomeCode)
            else str(outcome)
        )
        if outcome_text == SpendBudgetOverSignalOutcomeCode.REJECTED:
            return {
                "spend_budget_over_signal_contract_version": (
                    self.spend_budget_over_signal_contract_version
                ),
                "spend_budget_over_signal_outcome_code": outcome_text,
                "rejection_reason_code": self.rejection_reason_text(),
            }
        if (
            outcome_text != SpendBudgetOverSignalOutcomeCode.ACCEPTED
            and outcome_text != SpendBudgetOverSignalOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported spend budget over signal outcome: {outcome_text}")
        if (
            self.spend_budget_id is None
            or self.budget_amount is None
            or self.over_amount is None
            or self.window_started_at is None
            or self.window_ended_at is None
        ):
            raise ValueError("accepted over signals must include identity and amount")
        return {
            "spend_budget_over_signal_contract_version": (
                self.spend_budget_over_signal_contract_version
            ),
            "spend_budget_over_signal_outcome_code": outcome_text,
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "over_amount": format_exact_decimal(self.over_amount),
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
        """Return the thin ``spend_budget.over`` facts for the #24 envelope.

        The payload is a reference plus hash and exact over amount.  Remaining,
        rated lines, PII, PAN, secrets, raw documents, and statutory
        identifiers are omitted so currencies stay unmixed.
        """
        if self.spend_budget_id is None or self.billing_account_id is None:
            raise ValueError("rejected spend budget over signal has no webhook event data")
        if self.utilization_status != UTILIZATION_OVER:
            raise ValueError("only over observations have webhook event data")
        if self.over_amount is None:
            raise ValueError("accepted over signals must include over_amount")
        return {
            "spend_budget_id": str(self.spend_budget_id),
            "billing_account_id": str(self.billing_account_id),
            "source_payload_hash": self.source_payload_hash,
            "spend_budget_contract_version": self.spend_budget_contract_version,
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "over_amount": format_exact_decimal(self.over_amount),
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": SPEND_BUDGET_STATUS,
            "utilization_status": UTILIZATION_OVER,
        }


class SpendBudgetOverSignalService:
    """Observe one published budget and enqueue ``spend_budget.over`` once."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._evaluations = SpendBudgetEvaluationPresentmentService(self.ledger)

    def observe_spend_budget_over(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetOverSignalResult:
        """Observe one published budget and enqueue over only when utilization is over.

        Same-tenant published budgets return ``accepted`` or
        ``duplicate_replay``.  under and at write zero over-signal rows.
        Replay of the same ``spend_budget_id`` source does not enqueue a
        second row.  A crash after compute and before enqueue is healed
        by the next observe.
        """
        boundary = getattr(self.ledger, "transaction", nullcontext)
        with boundary():
            return self._observe_spend_budget_over_in_boundary(tenant_reference, spend_budget_id)

    def _observe_spend_budget_over_in_boundary(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetOverSignalResult:
        """Evaluate and maybe enqueue inside the ledger boundary."""
        try:
            evaluation = self._evaluations.present_spend_budget_evaluation(
                tenant_reference, spend_budget_id
            )
        except SpendBudgetEvaluationPresentmentQueryError as error:
            return _rejected(_rejection_for(error.rejection_reason_code))
        utilization_status = evaluation.utilization_status
        if utilization_status == UTILIZATION_UNDER or utilization_status == UTILIZATION_AT:
            return _from_evaluation(
                evaluation,
                SpendBudgetOverSignalOutcomeCode.ACCEPTED,
                _require_stored_budget_hash(self.ledger, evaluation.spend_budget_id),
            )
        if utilization_status != UTILIZATION_OVER:
            raise ValueError(f"unsupported utilization status: {utilization_status}")
        budget = self.ledger.get_spend_budget(evaluation.spend_budget_id)
        if budget is None:
            raise ValueError("accepted over signals require a stored budget")
        existing = _existing_over_event(
            self.ledger, budget.tenant_account_id, evaluation.spend_budget_id
        )
        outcome = (
            SpendBudgetOverSignalOutcomeCode.DUPLICATE_REPLAY
            if existing is not None
            else SpendBudgetOverSignalOutcomeCode.ACCEPTED
        )
        result = _from_evaluation(evaluation, outcome, budget.source_payload_hash)
        if existing is None:
            _enqueue_spend_budget_over(self.ledger, tenant_reference, result, self._clock())
        return result


def _enqueue_spend_budget_over(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: SpendBudgetOverSignalResult,
    occurred_at: datetime,
) -> None:
    """Append one ``spend_budget.over`` outbox row for a stored over observation.

    Replay of the same tenant, event type, ``spend_budget_id``, and payload
    hash returns the stored row.  Callers that already found a source_id
    row skip this helper so a later distinct over amount cannot grow the
    outbox.
    """
    if result.spend_budget_id is None or result.over_amount is None:
        raise ValueError("accepted over signals must include identity and over_amount")
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_SPEND_BUDGET_OVER,
        result.spend_budget_id,
        result.as_webhook_event_data(),
        occurred_at,
    )


def _existing_over_event(
    ledger: MemoryUsageLedger, tenant_account_id: UUID, spend_budget_id: UUID
):
    """Return the first ``spend_budget.over`` row for one source, if any."""
    for event in ledger.list_webhook_outbox_events_for_tenant(tenant_account_id):
        if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER and event.source_id == spend_budget_id:
            return event
    return None


def _require_stored_budget_hash(ledger: MemoryUsageLedger, spend_budget_id: UUID) -> str:
    """Return the stored source-payload hash for one published budget."""
    budget = ledger.get_spend_budget(spend_budget_id)
    if budget is None:
        raise ValueError("accepted over signals require a stored budget")
    return budget.source_payload_hash


def _rejection_for(rejection_reason_code: str) -> SpendBudgetRejectionReasonCode:
    """Map an evaluation query failure onto the write rejection vocabulary."""
    try:
        return SpendBudgetRejectionReasonCode(rejection_reason_code)
    except ValueError:
        return SpendBudgetRejectionReasonCode.REQUEST_INVALID


def _rejected(reason: SpendBudgetRejectionReasonCode) -> SpendBudgetOverSignalResult:
    """Return a sparse rejected over-signal result."""
    return SpendBudgetOverSignalResult(
        spend_budget_over_signal_outcome_code=SpendBudgetOverSignalOutcomeCode.REJECTED,
        spend_budget_over_signal_contract_version=SPEND_BUDGET_OVER_SIGNAL_CONTRACT_VERSION,
        spend_budget_id=None,
        tenant_reference=None,
        billing_account_id=None,
        currency_code=None,
        budget_amount=None,
        over_amount=None,
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
    outcome: SpendBudgetOverSignalOutcomeCode,
    source_payload_hash: str,
) -> SpendBudgetOverSignalResult:
    """Project one evaluation into the buyer-facing over-signal result."""
    return SpendBudgetOverSignalResult(
        spend_budget_over_signal_outcome_code=outcome,
        spend_budget_over_signal_contract_version=SPEND_BUDGET_OVER_SIGNAL_CONTRACT_VERSION,
        spend_budget_id=evaluation.spend_budget_id,
        tenant_reference=evaluation.tenant_reference,
        billing_account_id=evaluation.billing_account_id,
        currency_code=evaluation.currency_code,
        budget_amount=evaluation.budget_amount,
        over_amount=evaluation.over_amount,
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
