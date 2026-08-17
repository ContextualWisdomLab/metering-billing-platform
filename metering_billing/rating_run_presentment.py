"""Tenant-scoped rating-run presentment projected from stored rating facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``rating_run``.
3. Project window, totals, lines, and the next action.
4. Return the run.  Do not rate, draft, or invent a journal.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing.errors import RatingRunPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredRatingRun


RATING_RUN_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_DRAFT_INVOICE = "draft_invoice"


def next_operator_action() -> str:
    """Return draft_invoice.  Rate a window, then draft an invoice."""
    return OPERATOR_ACTION_DRAFT_INVOICE


@dataclass(frozen=True)
class RatingRunLinePresentment:
    """One stored rating line projected for operator presentment."""

    line_number: int
    billing_account_reference: str
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object for one rating line."""
        return {
            "line_number": self.line_number,
            "billing_account_reference": self.billing_account_reference,
            "meter_code": self.meter_code,
            "unit_code": self.unit_code,
            "rated_quantity": format_exact_decimal(self.rated_quantity),
            "unit_price_amount": format_exact_decimal(self.unit_price_amount),
            "line_total_amount": format_exact_decimal(self.line_total_amount),
        }


@dataclass(frozen=True)
class RatingRunPresentmentResult:
    """Buyer-facing projection of one stored rating run."""

    rating_run_id: UUID
    tenant_reference: str
    rate_card_code: str
    rate_card_version: int
    window_started_at: datetime
    window_ended_at: datetime
    usage_snapshot_hash: str
    currency_code: str
    rated_total_amount: Decimal
    recorded_at: datetime
    next_operator_action: str
    rating_lines: tuple[RatingRunLinePresentment, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "rating_run_presentment_contract_version": RATING_RUN_PRESENTMENT_CONTRACT_VERSION,
            "rating_run_id": str(self.rating_run_id),
            "tenant_reference": self.tenant_reference,
            "rate_card_code": self.rate_card_code,
            "rate_card_version": self.rate_card_version,
            "window_started_at": _format_recorded_at(self.window_started_at),
            "window_ended_at": _format_recorded_at(self.window_ended_at),
            "usage_snapshot_hash": self.usage_snapshot_hash,
            "currency_code": self.currency_code,
            "rated_total_amount": format_exact_decimal(self.rated_total_amount),
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
            "rating_lines": [item.as_contract_dict() for item in self.rating_lines],
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/rating-runs``."""
        return {
            "rating_run_id": str(self.rating_run_id),
            "rated_total_amount": format_exact_decimal(self.rated_total_amount),
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class RatingRunPresentmentPage:
    """One tenant-scoped page of rating-run summaries."""

    rating_runs: tuple[RatingRunPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{rating_runs, next_cursor}`` with summary items."""
        return {
            "rating_runs": [item.as_summary_dict() for item in self.rating_runs],
            "next_cursor": self.next_cursor,
        }


class RatingRunPresentmentService:
    """Read-only projector of stored rating runs into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_rating_run(
        self, tenant_reference: str, rating_run_id: UUID
    ) -> RatingRunPresentmentResult:
        """Return one same-tenant stored run, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not rate, draft, or invent a journal.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_rating_run(rating_run_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise RatingRunPresentmentQueryError("rating_run_not_found")
        return self._project_run(tenant.tenant_reference, stored)

    def list_rating_runs(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> RatingRunPresentmentPage:
        """Return one tenant page of rating summaries without mutating rating.

        Order is ``recorded_at`` then ``rating_run_id``.  The envelope is
        ``rating_runs`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_rating_runs(tenant.tenant_account_id),
            key=lambda run: (run.recorded_at, run.rating_run_id),
        )
        matched: list[StoredRatingRun] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.recorded_at, stored.rating_run_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.recorded_at, last.rating_run_id)
        return RatingRunPresentmentPage(
            rating_runs=tuple(
                self._project_run(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise RatingRunPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_run(
        self, tenant_reference: str, stored: StoredRatingRun
    ) -> RatingRunPresentmentResult:
        """Project one stored run using only persisted commercial fields."""
        return RatingRunPresentmentResult(
            rating_run_id=stored.rating_run_id,
            tenant_reference=tenant_reference,
            rate_card_code=stored.rate_card_code,
            rate_card_version=stored.rate_card_version,
            window_started_at=stored.window_started_at,
            window_ended_at=stored.window_ended_at,
            usage_snapshot_hash=stored.usage_snapshot_hash,
            currency_code=stored.currency_code,
            rated_total_amount=stored.rated_total_amount,
            recorded_at=stored.recorded_at,
            next_operator_action=next_operator_action(),
            rating_lines=tuple(
                RatingRunLinePresentment(
                    line_number=line.line_number,
                    billing_account_reference=line.billing_account_reference,
                    meter_code=line.meter_code,
                    unit_code=line.unit_code,
                    rated_quantity=line.rated_quantity,
                    unit_price_amount=line.unit_price_amount,
                    line_total_amount=line.line_total_amount,
                )
                for line in stored.rating_lines
            ),
        )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Render a rating timestamp as a timezone-aware ISO 8601 instant."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise RatingRunPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise RatingRunPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise RatingRunPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(recorded_at: datetime, rating_run_id: UUID) -> str:
    """Encode the keyset cursor as recorded_at then rating_run_id."""
    return f"{_format_recorded_at(recorded_at)}|{rating_run_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        recorded_text, run_text = cursor.split("|", 1)
        return parse_iso8601_datetime(recorded_text), UUID(run_text)
    except (TypeError, ValueError) as error:
        raise RatingRunPresentmentQueryError("request_invalid") from error
