"""Tenant-scoped spend-budget evaluation projected from stored commercial facts.

The service is a read path:

1. Resolve the tenant pin.
2. Load that tenant's stored ``spend_budget``.
3. Pin the commercial ``tenant_account`` to the budget's billing account.
4. Reuse ``RatedSpendPresentmentService`` with ``group_by=product``.
5. Sum already-rated amounts in the budget currency only.
6. Return exact remaining, over, and utilization.  Do not persist, hard-stop,
   post, or call AIS.

IFRS 15 treats a commercial budget as control evidence, not collected revenue
(IFRS Foundation, 2024).  IAS 21 requires source currency to stay unmixed
(IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).  IEEE 754 forbids smuggling binary floating-point
values into money (IEEE, 2019).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import (
    RatedSpendPresentmentQueryError,
    SpendBudgetEvaluationPresentmentQueryError,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal, require_decimal_quantity
from metering_billing.rated_spend_presentment import (
    GROUP_BY_PRODUCT,
    RatedSpendPresentmentService,
)
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import BillingAccount, MemoryUsageLedger, StoredSpendBudget


SPEND_BUDGET_EVALUATION_PRESENTMENT_CONTRACT_VERSION = 1
ZERO = Decimal("0")
OPERATOR_ACTION_WAIT = "wait"
UTILIZATION_UNDER = "under"
UTILIZATION_AT = "at"
UTILIZATION_OVER = "over"


def next_operator_action() -> str:
    """Return wait.  Evaluation is a safe compare and does not hard-stop work."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class SpendBudgetEvaluationPresentmentResult:
    """Buyer-facing comparison of one published budget to already-rated spend."""

    spend_budget_id: UUID
    tenant_reference: str
    billing_account_id: UUID
    currency_code: str
    budget_amount: Decimal
    rated_amount: Decimal
    remaining_amount: Decimal
    over_amount: Decimal
    utilization_status: str
    window_started_at: datetime
    window_ended_at: datetime
    spend_budget_status: str
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the evaluation schema."""
        return {
            "spend_budget_evaluation_presentment_contract_version": (
                SPEND_BUDGET_EVALUATION_PRESENTMENT_CONTRACT_VERSION
            ),
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "rated_amount": format_exact_decimal(self.rated_amount),
            "remaining_amount": format_exact_decimal(self.remaining_amount),
            "over_amount": format_exact_decimal(self.over_amount),
            "utilization_status": self.utilization_status,
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "spend_budget_status": self.spend_budget_status,
            "next_operator_action": self.next_operator_action,
        }


class SpendBudgetEvaluationPresentmentService:
    """Read-only projector that compares one published budget to rated spend."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._rated_spend = RatedSpendPresentmentService(self.ledger)

    def present_spend_budget_evaluation(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetEvaluationPresentmentResult:
        """Return one same-tenant budget evaluation, or fail closed.

        A missing or cross-tenant budget is indistinguishable.  A stored
        budget whose billing account belongs to another commercial
        ``tenant_account`` is forbidden.  The read does not mutate the
        budget, rate usage, or compose a journal.
        """
        tenant = self._require_tenant(tenant_reference)
        budget = self.ledger.get_spend_budget(spend_budget_id)
        if budget is None or budget.tenant_account_id != tenant.tenant_account_id:
            raise SpendBudgetEvaluationPresentmentQueryError("spend_budget_not_found")
        account = _billing_account_for(self.ledger, budget.billing_account_id)
        if account is None:
            raise SpendBudgetEvaluationPresentmentQueryError("billing_account_not_found")
        if account.tenant_account_id != tenant.tenant_account_id:
            raise SpendBudgetEvaluationPresentmentQueryError("billing_account_forbidden")
        window = TimeWindow(budget.window_started_at, budget.window_ended_at)
        try:
            spend = self._rated_spend.present_rated_spend(
                tenant.tenant_reference,
                account.billing_account_id,
                window,
                group_by=GROUP_BY_PRODUCT,
            )
        except RatedSpendPresentmentQueryError as error:
            raise SpendBudgetEvaluationPresentmentQueryError(
                error.rejection_reason_code
            ) from error
        budget_amount = require_decimal_quantity(budget.budget_amount)
        rated_amount = _same_currency_rated_amount(spend.products, budget.currency_code)
        remaining_amount, over_amount, utilization_status = _utilization(
            budget_amount, rated_amount
        )
        return self._project_evaluation(
            tenant.tenant_reference,
            budget,
            account,
            budget_amount,
            rated_amount,
            remaining_amount,
            over_amount,
            utilization_status,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise SpendBudgetEvaluationPresentmentQueryError("tenant_not_found")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise SpendBudgetEvaluationPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_evaluation(
        self,
        tenant_reference: str,
        budget: StoredSpendBudget,
        account: BillingAccount,
        budget_amount: Decimal,
        rated_amount: Decimal,
        remaining_amount: Decimal,
        over_amount: Decimal,
        utilization_status: str,
    ) -> SpendBudgetEvaluationPresentmentResult:
        """Project one stored budget plus already-rated spend into the statement."""
        return SpendBudgetEvaluationPresentmentResult(
            spend_budget_id=budget.spend_budget_id,
            tenant_reference=tenant_reference,
            billing_account_id=account.billing_account_id,
            currency_code=budget.currency_code,
            budget_amount=budget_amount,
            rated_amount=rated_amount,
            remaining_amount=remaining_amount,
            over_amount=over_amount,
            utilization_status=utilization_status,
            window_started_at=budget.window_started_at,
            window_ended_at=budget.window_ended_at,
            spend_budget_status="published",
            next_operator_action=next_operator_action(),
        )


def _billing_account_for(
    ledger: MemoryUsageLedger, billing_account_id: UUID
) -> BillingAccount | None:
    """Return the stored billing account for one internal identifier, if any."""
    for account in ledger.billing_accounts.values():
        if account.billing_account_id == billing_account_id:
            return account
    return None


def _same_currency_rated_amount(products: tuple, currency_code: str) -> Decimal:
    """Sum exclusive-account rated rows in the budget currency only."""
    total = ZERO
    for row in products:
        if row.currency_code != currency_code:
            continue
        total += require_decimal_quantity(row.rated_amount)
    return total


def _utilization(
    budget_amount: Decimal, rated_amount: Decimal
) -> tuple[Decimal, Decimal, str]:
    """Return complementary remaining/over amounts and utilization status."""
    if rated_amount < budget_amount:
        return budget_amount - rated_amount, ZERO, UTILIZATION_UNDER
    if rated_amount == budget_amount:
        return ZERO, ZERO, UTILIZATION_AT
    return ZERO, rated_amount - budget_amount, UTILIZATION_OVER


def _format_instant(instant: datetime) -> str:
    """Render one timezone-aware instant as ISO 8601 with a ``Z`` suffix."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
