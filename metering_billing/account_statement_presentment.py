"""Billing-account statement projected from already stored commercial facts.

The service is a read path:

1. Resolve the tenant pin.
2. Resolve the billing account by internal identifier.
3. Attribute money only through invoice-draft lines that belong exclusively
   to that account.
4. Roll stored issued-invoice totals, open collection remaining, applied
   credit-note amounts, write-offs, currently parked leftover, and refunded
   leftover by ``currency_code``.
5. Return one statement document.  Do not capture, credit, post, or call AIS.

A draft with no lines, or lines for more than one billing account, is omitted
so this read cannot invent a split that is not stored.  Parked leftover that
already has an apply or refund row is no longer unused cash.

IFRS 15 treats remaining consideration as presentation, not collected
revenue (IFRS Foundation, 2024).  IAS 21 requires source currency to stay
unmixed (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe,
idempotent read (Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_SETTLED_STATUS,
    parse_collection_amount,
)
from metering_billing.errors import AccountStatementPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import BillingAccount, MemoryUsageLedger, StoredInvoiceDraft


Clock = Callable[[], datetime]
ACCOUNT_STATEMENT_PRESENTMENT_CONTRACT_VERSION = 1
ZERO = Decimal("0")
ISSUED_INVOICE_TOTAL = "issued_invoice_total"
OPEN_COLLECTION_REMAINING = "open_collection_remaining"
APPLIED_CREDIT_TOTAL = "applied_credit_total"
WRITE_OFF_TOTAL = "write_off_total"
PARKED_UNAPPLIED_CASH = "parked_unapplied_cash"
REFUNDED_UNAPPLIED_CASH = "refunded_unapplied_cash"
CURRENCY_AMOUNT_FIELDS = (
    ISSUED_INVOICE_TOTAL,
    OPEN_COLLECTION_REMAINING,
    APPLIED_CREDIT_TOTAL,
    WRITE_OFF_TOTAL,
    PARKED_UNAPPLIED_CASH,
    REFUNDED_UNAPPLIED_CASH,
)


@dataclass
class _CurrencyTotals:
    """Mutable exact totals for one currency while the statement is folded."""

    issued_invoice_total: Decimal = field(default_factory=lambda: ZERO)
    open_collection_remaining: Decimal = field(default_factory=lambda: ZERO)
    applied_credit_total: Decimal = field(default_factory=lambda: ZERO)
    write_off_total: Decimal = field(default_factory=lambda: ZERO)
    parked_unapplied_cash: Decimal = field(default_factory=lambda: ZERO)
    refunded_unapplied_cash: Decimal = field(default_factory=lambda: ZERO)

    def has_activity(self) -> bool:
        """Return whether any stored bucket for this currency is positive."""
        return any(
            amount > ZERO
            for amount in (
                self.issued_invoice_total,
                self.open_collection_remaining,
                self.applied_credit_total,
                self.write_off_total,
                self.parked_unapplied_cash,
                self.refunded_unapplied_cash,
            )
        )


@dataclass(frozen=True)
class AccountStatementCurrencyResult:
    """One currency's stored commercial rollup.  Currencies are never mixed."""

    currency_code: str
    issued_invoice_total: Decimal
    open_collection_remaining: Decimal
    applied_credit_total: Decimal
    write_off_total: Decimal
    parked_unapplied_cash: Decimal
    refunded_unapplied_cash: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published for one currency."""
        return {
            "currency_code": self.currency_code,
            ISSUED_INVOICE_TOTAL: format_exact_decimal(self.issued_invoice_total),
            OPEN_COLLECTION_REMAINING: format_exact_decimal(self.open_collection_remaining),
            APPLIED_CREDIT_TOTAL: format_exact_decimal(self.applied_credit_total),
            WRITE_OFF_TOTAL: format_exact_decimal(self.write_off_total),
            PARKED_UNAPPLIED_CASH: format_exact_decimal(self.parked_unapplied_cash),
            REFUNDED_UNAPPLIED_CASH: format_exact_decimal(self.refunded_unapplied_cash),
        }


@dataclass(frozen=True)
class AccountStatementPresentmentResult:
    """Buyer-facing commercial statement for one billing account."""

    tenant_reference: str
    billing_account_id: UUID
    billing_account_reference: str
    as_of: datetime
    currencies: tuple[AccountStatementCurrencyResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the statement schema."""
        return {
            "account_statement_presentment_contract_version": (
                ACCOUNT_STATEMENT_PRESENTMENT_CONTRACT_VERSION
            ),
            "tenant_reference": self.tenant_reference,
            "billing_account_id": str(self.billing_account_id),
            "billing_account_reference": self.billing_account_reference,
            "as_of": _format_as_of(self.as_of),
            "currencies": [item.as_contract_dict() for item in self.currencies],
        }


class AccountStatementPresentmentService:
    """Read-only projector of stored commercial facts onto one billing account."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def present_account_statement(
        self, tenant_reference: str, billing_account_id: UUID
    ) -> AccountStatementPresentmentResult:
        """Return one billing-account statement, or fail closed.

        Missing tenant is ``tenant_not_found``.  Unknown account is
        ``billing_account_not_found``.  An account stored for another tenant
        is ``billing_account_forbidden``.  The read does not change money,
        proposal, or outbox rows.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise AccountStatementPresentmentQueryError("tenant_not_found")
        tenant = require_resolved(tenant, "tenant")
        account = _billing_account_for(self.ledger, billing_account_id)
        if account is None:
            raise AccountStatementPresentmentQueryError("billing_account_not_found")
        if account.tenant_account_id != tenant.tenant_account_id:
            raise AccountStatementPresentmentQueryError("billing_account_forbidden")
        draft_ids = _exclusive_draft_ids(
            self.ledger.list_invoice_drafts(tenant.tenant_account_id),
            account.billing_account_id,
        )
        account_case_ids = {
            stored.collection_case_id
            for stored in self.ledger.list_collection_cases(tenant.tenant_account_id)
            if stored.invoice_draft_id in draft_ids
        }
        consumed_leftover_ids = {
            application.unapplied_cash_id
            for application in self.ledger.list_unapplied_cash_applications_for_tenant(
                tenant.tenant_account_id
            )
        }
        consumed_leftover_ids.update(
            refund.unapplied_cash_id
            for refund in self.ledger.list_unapplied_cash_refunds_for_tenant(
                tenant.tenant_account_id
            )
        )
        totals: dict[str, _CurrencyTotals] = {}
        for invoice in self.ledger.list_issued_invoices_for_tenant(tenant.tenant_account_id):
            if invoice.invoice_draft_id not in draft_ids:
                continue
            currency_totals = totals.setdefault(invoice.currency_code, _CurrencyTotals())
            currency_totals.issued_invoice_total += parse_collection_amount(
                invoice.tax_inclusive_amount
            )
        for stored in self.ledger.list_collection_cases(tenant.tenant_account_id):
            if stored.invoice_draft_id not in draft_ids:
                continue
            if stored.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
                continue
            outstanding = parse_collection_amount(stored.outstanding_amount)
            if outstanding <= ZERO:
                continue
            currency_totals = totals.setdefault(stored.currency_code, _CurrencyTotals())
            currency_totals.open_collection_remaining += outstanding
        for application in self.ledger.list_credit_note_applications_for_tenant(
            tenant.tenant_account_id
        ):
            if application.invoice_draft_id not in draft_ids:
                continue
            currency_totals = totals.setdefault(application.currency_code, _CurrencyTotals())
            currency_totals.applied_credit_total += parse_collection_amount(
                application.applied_amount
            )
        for write_off in self.ledger.list_collection_write_offs_for_tenant(
            tenant.tenant_account_id
        ):
            if write_off.invoice_draft_id not in draft_ids:
                continue
            currency_totals = totals.setdefault(write_off.currency_code, _CurrencyTotals())
            currency_totals.write_off_total += parse_collection_amount(
                write_off.write_off_amount
            )
        for leftover in self.ledger.list_unapplied_cash_for_tenant(tenant.tenant_account_id):
            if leftover.collection_case_id not in account_case_ids:
                continue
            if leftover.unapplied_cash_id in consumed_leftover_ids:
                continue
            currency_totals = totals.setdefault(leftover.currency_code, _CurrencyTotals())
            currency_totals.parked_unapplied_cash += parse_collection_amount(
                leftover.unapplied_amount
            )
        for refund in self.ledger.list_unapplied_cash_refunds_for_tenant(
            tenant.tenant_account_id
        ):
            if refund.collection_case_id not in account_case_ids:
                continue
            currency_totals = totals.setdefault(refund.currency_code, _CurrencyTotals())
            currency_totals.refunded_unapplied_cash += parse_collection_amount(
                refund.refund_amount
            )
        currencies = tuple(
            AccountStatementCurrencyResult(
                currency_code=currency_code,
                issued_invoice_total=buckets.issued_invoice_total,
                open_collection_remaining=buckets.open_collection_remaining,
                applied_credit_total=buckets.applied_credit_total,
                write_off_total=buckets.write_off_total,
                parked_unapplied_cash=buckets.parked_unapplied_cash,
                refunded_unapplied_cash=buckets.refunded_unapplied_cash,
            )
            for currency_code, buckets in sorted(totals.items())
            if buckets.has_activity()
        )
        return AccountStatementPresentmentResult(
            tenant_reference=tenant.tenant_reference,
            billing_account_id=account.billing_account_id,
            billing_account_reference=account.billing_account_reference,
            as_of=self._clock(),
            currencies=currencies,
        )


def _billing_account_for(
    ledger: MemoryUsageLedger, billing_account_id: UUID
) -> BillingAccount | None:
    """Return the stored billing account for one internal identifier, if any."""
    for account in ledger.billing_accounts.values():
        if account.billing_account_id == billing_account_id:
            return account
    return None


def _exclusive_draft_ids(
    drafts: tuple[StoredInvoiceDraft, ...], billing_account_id: UUID
) -> set[UUID]:
    """Return drafts whose stored lines belong only to one billing account."""
    draft_ids: set[UUID] = set()
    for draft in drafts:
        line_accounts = {line.billing_account_id for line in draft.invoice_draft_lines}
        if line_accounts == {billing_account_id}:
            draft_ids.add(draft.invoice_draft_id)
    return draft_ids


def _format_as_of(as_of: datetime) -> str:
    """Render ``as_of`` as a timezone-aware ISO 8601 instant."""
    return as_of.astimezone(UTC).isoformat().replace("+00:00", "Z")
