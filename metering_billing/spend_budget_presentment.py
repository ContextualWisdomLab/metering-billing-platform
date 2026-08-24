"""Tenant-scoped spend-budget presentment projected from stored facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``spend_budget``.
3. Project identity, window, currency, exact amount, and the next action.
4. Return the budget.  Do not compare it to rated spend, post, or call AIS.

IFRS 15 treats a commercial budget as control evidence, not collected revenue
(IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import SpendBudgetPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredSpendBudget


SPEND_BUDGET_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"


def next_operator_action() -> str:
    """Return wait.  The budget is published; later slices may compare spend."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class SpendBudgetPresentmentResult:
    """Buyer-facing projection of one stored spend budget."""

    spend_budget_id: UUID
    tenant_reference: str
    billing_account_id: UUID
    currency_code: str
    budget_amount: Decimal
    window_started_at: datetime
    window_ended_at: datetime
    spend_budget_status: str
    source_payload_hash: str
    published_at: datetime
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "spend_budget_presentment_contract_version": (
                SPEND_BUDGET_PRESENTMENT_CONTRACT_VERSION
            ),
            "spend_budget_id": str(self.spend_budget_id),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "window_started_at": _format_published_at(self.window_started_at),
            "window_ended_at": _format_published_at(self.window_ended_at),
            "spend_budget_status": self.spend_budget_status,
            "source_payload_hash": self.source_payload_hash,
            "published_at": _format_published_at(self.published_at),
            "next_operator_action": self.next_operator_action,
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/spend-budgets``."""
        return {
            "spend_budget_id": str(self.spend_budget_id),
            "billing_account_id": str(self.billing_account_id),
            "currency_code": self.currency_code,
            "budget_amount": format_exact_decimal(self.budget_amount),
            "published_at": _format_published_at(self.published_at),
            "spend_budget_status": self.spend_budget_status,
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class SpendBudgetPresentmentPage:
    """One tenant-scoped page of spend-budget summaries."""

    spend_budgets: tuple[SpendBudgetPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{spend_budgets, next_cursor}`` with summary items."""
        return {
            "spend_budgets": [item.as_summary_dict() for item in self.spend_budgets],
            "next_cursor": self.next_cursor,
        }


class SpendBudgetPresentmentService:
    """Read-only projector of stored spend budgets into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_spend_budget(
        self, tenant_reference: str, spend_budget_id: UUID
    ) -> SpendBudgetPresentmentResult:
        """Return one same-tenant spend budget, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change budget status or compare rated spend.
        """
        tenant = self._require_tenant(tenant_reference)
        budget = self.ledger.get_spend_budget(spend_budget_id)
        if budget is None or budget.tenant_account_id != tenant.tenant_account_id:
            raise SpendBudgetPresentmentQueryError("spend_budget_not_found")
        return self._project_budget(tenant.tenant_reference, budget)

    def list_spend_budgets(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> SpendBudgetPresentmentPage:
        """Return one tenant page of spend-budget summaries without mutating rows.

        Order is ``published_at`` then ``spend_budget_id``.  The envelope is
        ``spend_budgets`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_spend_budgets(tenant.tenant_account_id),
            key=lambda budget: (budget.published_at, budget.spend_budget_id),
        )
        matched: list[StoredSpendBudget] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.published_at, stored.spend_budget_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.published_at, last.spend_budget_id)
        return SpendBudgetPresentmentPage(
            spend_budgets=tuple(
                self._project_budget(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise SpendBudgetPresentmentQueryError("tenant_not_found")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise SpendBudgetPresentmentQueryError("tenant_not_found")
        return require_resolved(tenant, "tenant")

    def _project_budget(
        self, tenant_reference: str, budget: StoredSpendBudget
    ) -> SpendBudgetPresentmentResult:
        """Project one stored spend budget using only persisted commercial fields."""
        return SpendBudgetPresentmentResult(
            spend_budget_id=budget.spend_budget_id,
            tenant_reference=tenant_reference,
            billing_account_id=budget.billing_account_id,
            currency_code=budget.currency_code,
            budget_amount=parse_invoice_amount(budget.budget_amount),
            window_started_at=budget.window_started_at,
            window_ended_at=budget.window_ended_at,
            spend_budget_status=budget.spend_budget_status,
            source_payload_hash=budget.source_payload_hash,
            published_at=budget.published_at,
            next_operator_action=next_operator_action(),
        )


def _format_published_at(published_at: datetime) -> str:
    """Render a stored instant as a timezone-aware ISO 8601 timestamp."""
    return published_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise SpendBudgetPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise SpendBudgetPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise SpendBudgetPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(published_at: datetime, spend_budget_id: UUID) -> str:
    """Encode the keyset cursor as published_at then spend_budget_id."""
    return f"{_format_published_at(published_at)}|{spend_budget_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        published_text, budget_text = cursor.split("|", 1)
        return parse_iso8601_datetime(published_text), UUID(budget_text)
    except (TypeError, ValueError) as error:
        raise SpendBudgetPresentmentQueryError("request_invalid") from error
