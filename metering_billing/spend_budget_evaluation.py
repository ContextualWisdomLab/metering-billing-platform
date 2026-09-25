"""Commercial spend-budget evaluation against already-rated spend.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``spend_budget``.
3. Reuse ``RatedSpendPresentmentService`` with ``group_by=product``.
4. Sum exclusive rated rows whose currency matches the budget.
5. Return under, at, or over.  Do not persist, post, or call AIS.

IFRS 15 treats a commercial budget as control evidence, not collected revenue
(IFRS Foundation, 2024).  IAS 21 requires source currency to stay unmixed
(IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Never
from uuid import UUID

from metering_billing.errors import (
    SpendBudgetEvaluationQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.rated_spend_presentment import (
    GROUP_BY_PRODUCT,
    RatedSpendPresentmentQueryError,
    RatedSpendPresentmentService,
)
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import MemoryUsageLedger, StoredSpendBudget


SPEND_BUDGET_EVALUATION_CONTRACT_VERSION = 1
OPERATOR_ACTION_WAIT = "wait"
ZERO = Decimal("0")
UTILIZATION_UNDER = "under"
UTILIZATION_AT = "at"
UTILIZATION_OVER = "over"
UtilizationStatus = Literal["under", "at", "over"]


def utilization_status(rated_amount: Decimal, budget_amount: Decimal) -> UtilizationStatus:
    """Return under, at, or over from exact rated and budget amounts."""
    if rated_amount < budget_amount:
        return UTILIZATION_UNDER
    if rated_amount > budget_amount:
        return UTILIZATION_OVER
    return UTILIZATION_AT


def remaining_and_over_amounts(
    status: UtilizationStatus, budget_amount: Decimal, rated_amount: Decimal
) -> tuple[Decimal, Decimal]:
    """Return remaining and over amounts for one closed utilization status."""
    if status == UTILIZATION_UNDER:
        return budget_amount - rated_amount, ZERO
    if status == UTILIZATION_AT:
        return ZERO, ZERO
    if status == UTILIZATION_OVER:
        return ZERO, rated_amount - budget_amount
    unreachable: Never = status
    raise ValueError(f"unsupported utilization status: {unreachable}")


def next_operator_action() -> str:
    """Return wait.  Evaluation is a read and does not invent a hard-stop write."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class SpendBudgetEvaluationResult:
    """Buyer-facing comparison of one published budget to exclusive rated spend."""

    spend_budget_id: UUID
    tenant_reference: str
    billing_account_id: UUID
    currency_code: str
    budget_amount: Decimal
    window_started_at: datetime
    window_ended_at: datetime
    spend_budget_status: str
    rated_amount: Decimal
    remaining_amount: Decimal
    over_amount: Decimal
    utilization_status: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the evaluation schema."""
        return {
            "spend_budget_evaluation_contract_version": (
                SPEND_BUDGET_EVALUATION_CONTRACT_VERSION
            ),
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": self.spend_budget_status,
            "rated_amount": format_exact_decimal(self.rated_amount),
            "remaining_amount": format_exact_decimal(self.remaining_amount),
            "over_amount": format_exact_decimal(self.over_amount),
            "utilization_status": self.utilization_status,
            "next_operator_action": self.next_operator_action,
        }


class SpendBudgetEvaluationService:
    """Read-only projector of one spend budget against already-rated spend."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def evaluate_spend_budget(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetEvaluationResult:
        """Return under, at, or over for one same-tenant published budget.

        Rated money comes only from ``RatedSpendPresentmentService`` with
        ``group_by=product``.  Other currencies are omitted.  Unrated usage
        is omitted.  The read does not persist an evaluation row.
        """
        tenant = self._require_tenant(tenant_reference)
        budget = self.ledger.get_spend_budget(spend_budget_id)
        if budget is None or budget.tenant_account_id != tenant.tenant_account_id:
            raise SpendBudgetEvaluationQueryError("spend_budget_not_found")
        rated_amount = self._rated_amount_for(tenant.tenant_reference, budget)
        budget_amount = parse_invoice_amount(budget.budget_amount)
        status = utilization_status(rated_amount, budget_amount)
        remaining_amount, over_amount = remaining_and_over_amounts(
            status, budget_amount, rated_amount
        )
        return SpendBudgetEvaluationResult(
            spend_budget_id=budget.spend_budget_id,
            tenant_reference=tenant.tenant_reference,
            billing_account_id=budget.billing_account_id,
            currency_code=budget.currency_code,
            budget_amount=budget_amount,
            window_started_at=budget.window_started_at,
            window_ended_at=budget.window_ended_at,
            spend_budget_status="published",
            rated_amount=rated_amount,
            remaining_amount=remaining_amount,
            over_amount=over_amount,
            utilization_status=status,
            next_operator_action=next_operator_action(),
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise SpendBudgetEvaluationQueryError("tenant_not_found")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise SpendBudgetEvaluationQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _rated_amount_for(self, tenant_reference: str, budget: StoredSpendBudget) -> Decimal:
        """Sum exclusive product rows in the budget currency, or zero."""
        window = TimeWindow(budget.window_started_at, budget.window_ended_at)
        try:
            spend = RatedSpendPresentmentService(self.ledger).present_rated_spend(
                tenant_reference,
                budget.billing_account_id,
                window,
                group_by=GROUP_BY_PRODUCT,
            )
        except RatedSpendPresentmentQueryError as error:
            raise SpendBudgetEvaluationQueryError(
                _map_rated_spend_error(error.rejection_reason_code)
            ) from error
        total = ZERO
        for row in spend.products:
            if row.currency_code == budget.currency_code:
                total += row.rated_amount
        return total


def _map_rated_spend_error(reason: str) -> str:
    """Map rated-spend fail-closed codes onto the evaluation read."""
    if reason == "tenant_not_found":
        return "tenant_not_found"
    if reason == "billing_account_not_found":
        return "request_invalid"
    if reason == "billing_account_forbidden":
        return "request_invalid"
    if reason == "request_invalid":
        return "request_invalid"
    return "request_invalid"


def _format_instant(instant: datetime) -> str:
    """Render a stored instant as a timezone-aware ISO 8601 timestamp."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
