"""Tenant-scoped approaching-signal presentment from live evaluation and stored outbox.

The service is a read path:

1. Reuse ``SpendBudgetEvaluationPresentmentService`` for the same remaining
   and over math as ``GET /v1/spend-budgets/{id}/evaluation``.
2. Project the live observation into the existing approaching-signal envelope.
3. Project zero or one stored ``spend_budget.approaching`` webhook-outbox
   presentment for that ``spend_budget_id``.
4. Do not enqueue, persist, hard-stop, or compose a journal.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
IFRS 15 treats the observation as control evidence, not collected revenue
(IFRS Foundation, 2024).  IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019).
"""

from __future__ import annotations

from dataclasses import dataclass

from uuid import UUID

from metering_billing.errors import (
    SpendBudgetApproachingSignalOutcomeCode,
    SpendBudgetEvaluationPresentmentQueryError,
    SpendBudgetApproachingSignalPresentmentQueryError,
)
from metering_billing.spend_budget_evaluation_presentment import (
    SpendBudgetEvaluationPresentmentService,
    UTILIZATION_AT,
    UTILIZATION_OVER,
    UTILIZATION_UNDER,
)
from metering_billing.spend_budget_approaching_signal import (
    SpendBudgetApproachingSignalResult,
    _existing_approaching_event,
    _from_evaluation,
)
from metering_billing.usage_ledger import MemoryUsageLedger
from metering_billing.webhook_outbox_event_presentment import (
    WebhookOutboxEventPresentmentResult,
    WebhookOutboxEventPresentmentService,
)


SPEND_BUDGET_APPROACHING_SIGNAL_PRESENTMENT_CONTRACT_VERSION = 1
KNOWN_UTILIZATION = frozenset({UTILIZATION_UNDER, UTILIZATION_AT, UTILIZATION_OVER})


@dataclass(frozen=True)
class SpendBudgetApproachingSignalPresentmentResult:
    """Buyer-facing live approaching-signal plus stored first-at outbox rows."""

    approaching_signal: SpendBudgetApproachingSignalResult
    webhook_outbox_events: tuple[WebhookOutboxEventPresentmentResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object that nests the existing envelopes."""
        return {
            "spend_budget_approaching_signal_presentment_contract_version": (
                SPEND_BUDGET_APPROACHING_SIGNAL_PRESENTMENT_CONTRACT_VERSION
            ),
            "approaching_signal": self.approaching_signal.as_contract_dict(),
            "webhook_outbox_events": [
                item.as_contract_dict() for item in self.webhook_outbox_events
            ],
        }


class SpendBudgetApproachingSignalPresentmentService:
    """Read-only projector of one published budget's approaching-signal observation."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._evaluations = SpendBudgetEvaluationPresentmentService(self.ledger)
        self._outbox = WebhookOutboxEventPresentmentService(self.ledger)

    def present_spend_budget_approaching_signal(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetApproachingSignalPresentmentResult:
        """Return one same-tenant live approaching-signal and stored outbox rows.

        Same-tenant published budgets are accepted.  A missing or
        cross-tenant budget is indistinguishable.  A stored budget whose
        billing account belongs to another commercial ``tenant_account``
        is forbidden.  The read does not enqueue, mutate the budget, or
        compose a journal.
        """
        try:
            evaluation = self._evaluations.present_spend_budget_evaluation(
                tenant_reference, spend_budget_id
            )
        except SpendBudgetEvaluationPresentmentQueryError as error:
            raise SpendBudgetApproachingSignalPresentmentQueryError(
                error.rejection_reason_code
            ) from error
        if evaluation.utilization_status not in KNOWN_UTILIZATION:
            raise ValueError(
                f"unsupported utilization status: {evaluation.utilization_status}"
            )
        budget = self.ledger.get_spend_budget(evaluation.spend_budget_id)
        if budget is None:
            raise ValueError("accepted approaching-signal presentment requires a stored budget")
        approaching_signal = _from_evaluation(
            evaluation,
            SpendBudgetApproachingSignalOutcomeCode.ACCEPTED,
            budget.source_payload_hash,
        )
        existing = _existing_approaching_event(
            self.ledger, budget.tenant_account_id, evaluation.spend_budget_id
        )
        outbox: tuple[WebhookOutboxEventPresentmentResult, ...] = ()
        if existing is not None:
            outbox = (
                self._outbox._project_event(evaluation.tenant_reference, existing),
            )
        return SpendBudgetApproachingSignalPresentmentResult(approaching_signal, outbox)
