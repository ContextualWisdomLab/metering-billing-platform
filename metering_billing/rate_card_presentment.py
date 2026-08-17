"""Tenant-scoped rate-card presentment projected from stored catalog facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``rate_card`` and latest ``rate_card_version``.
3. Project unit prices, currency, and the next action.
4. Return the card.  Do not invent a catalog, journal, or default price.

TM Forum TMF620 treats a catalog as a versioned, queryable price list
(TM Forum, 2024).  RFC 9110 treats GET as a safe, idempotent read
(Fielding et al., 2022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import RateCardPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.rate_card import RateCardLineResult, _latest_version
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredRateCard


RATE_CARD_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_RATE_WINDOW = "rate_window"


def next_operator_action() -> str:
    """Return rate_window.  Publish a rate card, then rate that version."""
    return OPERATOR_ACTION_RATE_WINDOW


@dataclass(frozen=True)
class RateCardPresentmentResult:
    """Buyer-facing projection of one stored rate card and its latest version."""

    rate_card_id: UUID
    tenant_reference: str
    rate_card_name: str
    currency_code: str
    rate_card_version: int
    rate_card_version_id: UUID
    created_at: datetime
    published_at: datetime
    next_operator_action: str
    lines: tuple[RateCardLineResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        return {
            "rate_card_presentment_contract_version": RATE_CARD_PRESENTMENT_CONTRACT_VERSION,
            "rate_card_id": str(self.rate_card_id),
            "tenant_reference": self.tenant_reference,
            "rate_card_name": self.rate_card_name,
            "currency_code": self.currency_code,
            "rate_card_version": self.rate_card_version,
            "rate_card_version_id": str(self.rate_card_version_id),
            "created_at": _format_created_at(self.created_at),
            "published_at": _format_created_at(self.published_at),
            "next_operator_action": self.next_operator_action,
            "lines": [line.as_contract_dict() for line in self.lines],
        }

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by ``GET /v1/rate-cards``."""
        return {
            "rate_card_id": str(self.rate_card_id),
            "rate_card_name": self.rate_card_name,
            "currency_code": self.currency_code,
            "rate_card_version": self.rate_card_version,
            "created_at": _format_created_at(self.created_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class RateCardPresentmentPage:
    """One tenant-scoped page of rate-card summaries."""

    rate_cards: tuple[RateCardPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{rate_cards, next_cursor}`` with summary items."""
        return {
            "rate_cards": [item.as_summary_dict() for item in self.rate_cards],
            "next_cursor": self.next_cursor,
        }


class RateCardPresentmentService:
    """Read-only projector of stored rate cards into operator statements."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_rate_card(
        self, tenant_reference: str, rate_card_id: UUID
    ) -> RateCardPresentmentResult:
        """Return one same-tenant published card, or fail closed.

        A missing, header-only, or cross-tenant identifier is indistinguishable.
        The read does not publish a version or invent a price.
        """
        tenant = self._require_tenant(tenant_reference)
        card = self.ledger.get_rate_card(rate_card_id)
        if card is None or card.tenant_account_id != tenant.tenant_account_id:
            raise RateCardPresentmentQueryError("rate_card_not_found")
        projected = self._project_card(tenant.tenant_reference, card)
        if projected is None:
            raise RateCardPresentmentQueryError("rate_card_not_found")
        return projected

    def list_rate_cards(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> RateCardPresentmentPage:
        """Return one tenant page of published-card summaries.

        Order is ``created_at`` then ``rate_card_id``.  Header-only cards are
        omitted.  The envelope is ``rate_cards`` plus ``next_cursor`` only.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_rate_cards(tenant.tenant_account_id),
            key=lambda card: (card.created_at, card.rate_card_id),
        )
        matched: list[RateCardPresentmentResult] = []
        for stored in stored_rows:
            if cursor_key is not None and (stored.created_at, stored.rate_card_id) <= cursor_key:
                continue
            projected = self._project_card(tenant.tenant_reference, stored)
            if projected is None:
                continue
            matched.append(projected)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.created_at, last.rate_card_id)
        return RateCardPresentmentPage(rate_cards=tuple(page_rows), next_cursor=next_cursor)

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise RateCardPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_card(
        self, tenant_reference: str, card: StoredRateCard
    ) -> RateCardPresentmentResult | None:
        """Project one stored card using only the latest published version."""
        latest = _latest_version(self.ledger, card.tenant_account_id, card.rate_card_id)
        if latest is None:
            return None
        return RateCardPresentmentResult(
            rate_card_id=card.rate_card_id,
            tenant_reference=tenant_reference,
            rate_card_name=card.rate_card_name,
            currency_code=card.currency_code,
            rate_card_version=latest.version_number,
            rate_card_version_id=latest.rate_card_version_id,
            created_at=card.created_at,
            published_at=latest.published_at,
            next_operator_action=next_operator_action(),
            lines=tuple(
                RateCardLineResult(
                    metric_code=line.metric_code,
                    unit_amount=line.unit_amount,
                    currency_code=line.currency_code,
                )
                for line in latest.rate_card_lines
            ),
        )


def _format_created_at(created_at: datetime) -> str:
    """Render a catalog timestamp as a timezone-aware ISO 8601 instant."""
    return created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise RateCardPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise RateCardPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise RateCardPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(created_at: datetime, rate_card_id: UUID) -> str:
    """Encode the keyset cursor as created_at then rate_card_id."""
    return f"{_format_created_at(created_at)}|{rate_card_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        created_text, card_text = cursor.split("|", 1)
        return parse_iso8601_datetime(created_text), UUID(card_text)
    except (TypeError, ValueError) as error:
        raise RateCardPresentmentQueryError("request_invalid") from error
