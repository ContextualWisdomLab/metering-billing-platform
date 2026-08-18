"""Billing-account rated spend projected from already stored rating facts.

The service is a read path:

1. Resolve the tenant pin.
2. Resolve the billing account by internal identifier.
3. Keep stored ``rating_run`` rows whose half-open window equals the query
   window.
4. Take exclusive invoice-draft lines when that draft belongs only to the
   account.  Mixed-account and lineless drafts are omitted so this read
   cannot invent a split.  When no exclusive draft exists, take stored
   rating lines already attributed to that account.
5. Group those stored line amounts by ``product_code`` from usage events
   that belong exclusively to the account in the same window.  Mixed
   product codes omit the run so the read cannot invent a split.
   Optional ``group_by=project`` further keys rows by the one
   ``project_reference`` on exclusive-account usage that already has a
   project URN.  Usage without ``project_reference`` is omitted from that
   grouping.  Mixed projects omit the run so the read cannot invent a
   split or a sentinel project.
6. Return one presentment document.  Do not re-rate, invent a unit price,
   include unrated usage, capture, post, or call AIS.

IFRS 15 treats rated consideration as presentation, not collected revenue
(IFRS Foundation, 2024).  IAS 21 requires source currency to stay unmixed
(IFRS Foundation, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import RatedSpendPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal, require_decimal_quantity
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import (
    BillingAccount,
    MemoryUsageLedger,
    StoredInvoiceDraft,
    StoredRatingRun,
    StoredUsageEvent,
)


RATED_SPEND_PRESENTMENT_CONTRACT_VERSION = 1
ZERO = Decimal("0")
GROUP_BY_PRODUCT = "product"
GROUP_BY_PROJECT = "project"
ALLOWED_GROUP_BY = frozenset({GROUP_BY_PRODUCT, GROUP_BY_PROJECT})


@dataclass(frozen=True)
class RatedSpendProductResult:
    """One product's stored rated amount in one currency.  Currencies stay unmixed."""

    currency_code: str
    product_code: str
    rated_amount: Decimal
    project_reference: str | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published for one product row."""
        payload: dict[str, object] = {
            "currency_code": self.currency_code,
            "product_code": self.product_code,
            "rated_amount": format_exact_decimal(self.rated_amount),
        }
        if self.project_reference is not None:
            payload["project_reference"] = self.project_reference
        return payload


@dataclass(frozen=True)
class RatedSpendPresentmentResult:
    """Buyer-facing already-rated spend for one billing account and window."""

    tenant_reference: str
    billing_account_id: UUID
    billing_account_reference: str
    window_started_at: datetime
    window_ended_at: datetime
    products: tuple[RatedSpendProductResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the spend schema."""
        return {
            "rated_spend_presentment_contract_version": (
                RATED_SPEND_PRESENTMENT_CONTRACT_VERSION
            ),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "billing_account_reference": self.billing_account_reference,
            "window_started_at": _format_instant(self.window_started_at),
            "window_ended_at": _format_instant(self.window_ended_at),
            "products": [item.as_contract_dict() for item in self.products],
        }


class RatedSpendPresentmentService:
    """Read-only projector of stored rated line amounts onto one billing account."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_rated_spend(
        self,
        tenant_reference: str,
        billing_account_id: UUID,
        time_window: TimeWindow,
        group_by: str = GROUP_BY_PRODUCT,
    ) -> RatedSpendPresentmentResult:
        """Return already-rated spend for one account and window, or fail closed.

        Missing tenant is ``tenant_not_found``.  Unknown account is
        ``billing_account_not_found``.  An account stored for another tenant
        is ``billing_account_forbidden``.  Unknown ``group_by`` is
        ``request_invalid``.  The read does not change money, rating, draft,
        proposal, or outbox rows.
        """
        if group_by not in ALLOWED_GROUP_BY:
            raise RatedSpendPresentmentQueryError("request_invalid")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise RatedSpendPresentmentQueryError("tenant_not_found")
        tenant = require_resolved(tenant, "tenant")
        account = _billing_account_for(self.ledger, billing_account_id)
        if account is None:
            raise RatedSpendPresentmentQueryError("billing_account_not_found")
        if account.tenant_account_id != tenant.tenant_account_id:
            raise RatedSpendPresentmentQueryError("billing_account_forbidden")
        started = time_window.window_started_at.astimezone(UTC)
        ended = time_window.window_ended_at.astimezone(UTC)
        drafts_by_run = {
            draft.rating_run_id: draft
            for draft in self.ledger.list_invoice_drafts(tenant.tenant_account_id)
        }
        events = self.ledger.list_usage_events_in_window(
            tenant.tenant_account_id, started, ended
        )
        totals: dict[tuple[str, str, str | None], Decimal] = {}
        for rating_run in self.ledger.list_rating_runs(tenant.tenant_account_id):
            if rating_run.window_started_at.astimezone(UTC) != started:
                continue
            if rating_run.window_ended_at.astimezone(UTC) != ended:
                continue
            amount = _exclusive_rated_amount(
                rating_run,
                drafts_by_run.get(rating_run.rating_run_id),
                account.billing_account_id,
            )
            if amount <= ZERO:
                continue
            product_code = _exclusive_product_code(events, account.billing_account_id)
            if product_code is None:
                continue
            project_reference = None
            if group_by == GROUP_BY_PROJECT:
                project_reference = _exclusive_project_reference(
                    events, account.billing_account_id
                )
                if project_reference is None:
                    continue
            key = (rating_run.currency_code, product_code, project_reference)
            totals[key] = totals.get(key, ZERO) + amount
        products = tuple(
            RatedSpendProductResult(
                currency_code=currency_code,
                product_code=product_code,
                rated_amount=rated_amount,
                project_reference=project_reference,
            )
            for (currency_code, product_code, project_reference), rated_amount in sorted(
                totals.items()
            )
        )
        return RatedSpendPresentmentResult(
            tenant_reference=tenant.tenant_reference,
            billing_account_id=account.billing_account_id,
            billing_account_reference=account.billing_account_reference,
            window_started_at=started,
            window_ended_at=ended,
            products=products,
        )


def _billing_account_for(
    ledger: MemoryUsageLedger, billing_account_id: UUID
) -> BillingAccount | None:
    """Return the stored billing account for one internal identifier, if any."""
    for account in ledger.billing_accounts.values():
        if account.billing_account_id == billing_account_id:
            return account
    return None


def _exclusive_rated_amount(
    rating_run: StoredRatingRun,
    draft: StoredInvoiceDraft | None,
    billing_account_id: UUID,
) -> Decimal:
    """Return stored exclusive-line amounts, or zero when a split would be invented."""
    if draft is not None:
        line_accounts = {line.billing_account_id for line in draft.invoice_draft_lines}
        if line_accounts == {billing_account_id}:
            return sum(
                (require_decimal_quantity(line.line_total_amount) for line in draft.invoice_draft_lines),
                ZERO,
            )
    return sum(
        (
            require_decimal_quantity(line.line_total_amount)
            for line in rating_run.rating_lines
            if line.billing_account_id == billing_account_id
        ),
        ZERO,
    )


def _exclusive_product_code(
    events: tuple[StoredUsageEvent, ...], billing_account_id: UUID
) -> str | None:
    """Return the one product on exclusive-account usage, or None when mixed."""
    product_codes = {
        event.product_code
        for event in events
        if event.billing_account_id == billing_account_id
    }
    if len(product_codes) != 1:
        return None
    return next(iter(product_codes))


def _exclusive_project_reference(
    events: tuple[StoredUsageEvent, ...], billing_account_id: UUID
) -> str | None:
    """Return the one stored project URN on exclusive-account usage, or None."""
    project_references = {
        event.project_reference
        for event in events
        if event.billing_account_id == billing_account_id and event.project_reference
    }
    if len(project_references) != 1:
        return None
    return next(iter(project_references))


def _format_instant(instant: datetime) -> str:
    """Render one timezone-aware instant as ISO 8601 with a ``Z`` suffix."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
