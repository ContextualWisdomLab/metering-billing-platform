"""Tenant-scoped collection aging projected from stored open-case remaining.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``collection_case`` rows.
3. Age remaining outstanding by commercial due date into current / 1-30 /
   31-60 / 61-90 / 90+ buckets, grouped by ``currency_code``.
4. Return the totals.  Do not capture, credit, post, or call AIS.

Due date is the issued-invoice ``due_at`` for the same draft when stored,
otherwise the case ``opened_at`` (the only instant on ``collection_case``).
Settled cases and exact-zero remaining are omitted so write-offs do not
inflate aged dollars.

IFRS 15 treats remaining consideration as presentation, not collected
revenue (IFRS Foundation, 2024).  RFC 9110 treats GET as a safe,
idempotent read (Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_SETTLED_STATUS,
    parse_collection_amount,
)
from metering_billing.errors import CollectionAgingPresentmentQueryError, require_resolved
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import MemoryUsageLedger, StoredCollectionCase


Clock = Callable[[], datetime]
COLLECTION_AGING_PRESENTMENT_CONTRACT_VERSION = 1
ZERO = Decimal("0")
BUCKET_CURRENT = "current"
BUCKET_DAYS_1_30 = "days_1_30"
BUCKET_DAYS_31_60 = "days_31_60"
BUCKET_DAYS_61_90 = "days_61_90"
BUCKET_DAYS_90_PLUS = "days_90_plus"
AGING_BUCKET_CODES = (
    BUCKET_CURRENT,
    BUCKET_DAYS_1_30,
    BUCKET_DAYS_31_60,
    BUCKET_DAYS_61_90,
    BUCKET_DAYS_90_PLUS,
)


def aging_bucket_code(days_past_due: int) -> str:
    """Return the closed aging bucket for one non-negative day count.

    ``current`` is not yet due or due today.  Positive days are 1-30, 31-60,
    61-90, then 90+.
    """
    if days_past_due <= 0:
        return BUCKET_CURRENT
    if days_past_due <= 30:
        return BUCKET_DAYS_1_30
    if days_past_due <= 60:
        return BUCKET_DAYS_31_60
    if days_past_due <= 90:
        return BUCKET_DAYS_61_90
    return BUCKET_DAYS_90_PLUS


@dataclass(frozen=True)
class CollectionAgingBucketResult:
    """One currency bucket: case count and exact inclusive outstanding."""

    case_count: int
    outstanding_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published for one aging bucket."""
        return {
            "case_count": self.case_count,
            "outstanding_amount": format_exact_decimal(self.outstanding_amount),
        }


@dataclass(frozen=True)
class CollectionAgingCurrencyResult:
    """One currency's five aging buckets.  Currencies are never mixed."""

    currency_code: str
    current: CollectionAgingBucketResult
    days_1_30: CollectionAgingBucketResult
    days_31_60: CollectionAgingBucketResult
    days_61_90: CollectionAgingBucketResult
    days_90_plus: CollectionAgingBucketResult

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published for one currency."""
        return {
            "currency_code": self.currency_code,
            BUCKET_CURRENT: self.current.as_contract_dict(),
            BUCKET_DAYS_1_30: self.days_1_30.as_contract_dict(),
            BUCKET_DAYS_31_60: self.days_31_60.as_contract_dict(),
            BUCKET_DAYS_61_90: self.days_61_90.as_contract_dict(),
            BUCKET_DAYS_90_PLUS: self.days_90_plus.as_contract_dict(),
        }


@dataclass(frozen=True)
class CollectionAgingPresentmentResult:
    """Buyer-facing aging totals for one tenant."""

    tenant_reference: str
    as_of: datetime
    currencies: tuple[CollectionAgingCurrencyResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the aging schema."""
        return {
            "collection_aging_presentment_contract_version": (
                COLLECTION_AGING_PRESENTMENT_CONTRACT_VERSION
            ),
            "tenant_reference": self.tenant_reference,
            "as_of": _format_as_of(self.as_of),
            "currencies": [item.as_contract_dict() for item in self.currencies],
        }


class CollectionAgingPresentmentService:
    """Read-only projector of open-case remaining into AR aging buckets."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def present_collection_aging(self, tenant_reference: str) -> CollectionAgingPresentmentResult:
        """Return one tenant's aging totals, or fail closed.

        Settled cases and exact-zero remaining are omitted.  The read does
        not change case, dunning, draft, or proposal status.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CollectionAgingPresentmentQueryError("tenant_not_found")
        tenant = require_resolved(tenant, "tenant")
        as_of = self._clock()
        totals: dict[str, dict[str, CollectionAgingBucketResult]] = {}
        for stored in self.ledger.list_collection_cases(tenant.tenant_account_id):
            if not _is_ageable(stored):
                continue
            outstanding = parse_collection_amount(stored.outstanding_amount)
            if outstanding <= ZERO:
                continue
            due_at = self._due_at(tenant.tenant_account_id, stored)
            days_past_due = (as_of.astimezone(UTC).date() - due_at.astimezone(UTC).date()).days
            bucket_code = aging_bucket_code(days_past_due)
            currency_totals = totals.setdefault(
                stored.currency_code, _empty_currency_buckets()
            )
            current = currency_totals[bucket_code]
            currency_totals[bucket_code] = CollectionAgingBucketResult(
                case_count=current.case_count + 1,
                outstanding_amount=current.outstanding_amount + outstanding,
            )
        currencies = tuple(
            CollectionAgingCurrencyResult(
                currency_code=currency_code,
                current=buckets[BUCKET_CURRENT],
                days_1_30=buckets[BUCKET_DAYS_1_30],
                days_31_60=buckets[BUCKET_DAYS_31_60],
                days_61_90=buckets[BUCKET_DAYS_61_90],
                days_90_plus=buckets[BUCKET_DAYS_90_PLUS],
            )
            for currency_code, buckets in sorted(totals.items())
        )
        return CollectionAgingPresentmentResult(
            tenant_reference=tenant.tenant_reference,
            as_of=as_of,
            currencies=currencies,
        )

    def _due_at(self, tenant_account_id: UUID, stored: StoredCollectionCase) -> datetime:
        """Return issued-invoice due_at when stored, otherwise case opened_at."""
        issued = self.ledger.find_issued_invoice(tenant_account_id, stored.invoice_draft_id)
        if issued is not None and issued.due_at is not None:
            return issued.due_at
        return stored.opened_at


def _is_ageable(stored: StoredCollectionCase) -> bool:
    """Return whether the case is commercially open or in dunning."""
    return stored.collection_case_status != COLLECTION_CASE_SETTLED_STATUS


def _empty_currency_buckets() -> dict[str, CollectionAgingBucketResult]:
    """Return five zero buckets for one currency."""
    empty = CollectionAgingBucketResult(case_count=0, outstanding_amount=ZERO)
    return {
        BUCKET_CURRENT: empty,
        BUCKET_DAYS_1_30: empty,
        BUCKET_DAYS_31_60: empty,
        BUCKET_DAYS_61_90: empty,
        BUCKET_DAYS_90_PLUS: empty,
    }


def _format_as_of(as_of: datetime) -> str:
    """Render ``as_of`` as a timezone-aware ISO 8601 instant."""
    return as_of.astimezone(UTC).isoformat().replace("+00:00", "Z")
