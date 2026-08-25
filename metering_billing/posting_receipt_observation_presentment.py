"""Tenant-scoped posting-receipt observation presentment from stored facts.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``posting_receipt_observation``.
3. Project identity, AIS status, hashes, timestamps, and the next action.
4. Return the observation.  Do not pull AIS, drain, or flip proposal_status.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import PostingReceiptObservationPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredPostingReceiptObservation


POSTING_RECEIPT_OBSERVATION_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"


def next_operator_action() -> str:
    """Return wait.  The observation is stored; proposal_status stays validated."""
    return OPERATOR_ACTION_WAIT


@dataclass(frozen=True)
class PostingReceiptObservationPresentmentResult:
    """Buyer-facing projection of one stored posting-receipt observation."""

    posting_receipt_observation_id: UUID
    tenant_reference: str
    source_proposal_id: UUID
    idempotency_key: str
    receipt_id: UUID
    receipt_contract_version: int
    source_payload_hash: str
    posting_status_code: str
    recorded_at: str
    observed_at: datetime
    posted_at: str | None
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "posting_receipt_observation_presentment_contract_version": (
                POSTING_RECEIPT_OBSERVATION_PRESENTMENT_CONTRACT_VERSION
            ),
            "posting_receipt_observation_id": str(self.posting_receipt_observation_id),
            "tenant_reference": self.tenant_reference,
            "source_proposal_id": str(self.source_proposal_id),
            "idempotency_key": self.idempotency_key,
            "receipt_id": str(self.receipt_id),
            "receipt_contract_version": self.receipt_contract_version,
            "source_payload_hash": self.source_payload_hash,
            "posting_status_code": self.posting_status_code,
            "recorded_at": self.recorded_at,
            "observed_at": _format_observed_at(self.observed_at),
            "next_operator_action": self.next_operator_action,
        }
        if self.posted_at is not None:
            payload["posted_at"] = self.posted_at
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "posting_receipt_observation_id": str(self.posting_receipt_observation_id),
            "idempotency_key": self.idempotency_key,
            "posting_status_code": self.posting_status_code,
            "observed_at": _format_observed_at(self.observed_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class PostingReceiptObservationPresentmentPage:
    """One tenant-scoped page of posting-receipt observation summaries."""

    posting_receipt_observations: tuple[PostingReceiptObservationPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{posting_receipt_observations, next_cursor}`` with summaries."""
        return {
            "posting_receipt_observations": [
                item.as_summary_dict() for item in self.posting_receipt_observations
            ],
            "next_cursor": self.next_cursor,
        }


class PostingReceiptObservationPresentmentService:
    """Read-only projector of stored posting-receipt observations."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_posting_receipt_observation(
        self, tenant_reference: str, idempotency_key: str
    ) -> PostingReceiptObservationPresentmentResult:
        """Return one same-tenant stored observation, or fail closed.

        A missing or cross-tenant key is indistinguishable.  The read does
        not pull AIS, drain, or flip ``proposal_status``.
        """
        tenant = self._require_tenant(tenant_reference)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise PostingReceiptObservationPresentmentQueryError("observation_not_found")
        stored = self.ledger.find_posting_receipt_observation(
            tenant.tenant_account_id, idempotency_key
        )
        if stored is None:
            raise PostingReceiptObservationPresentmentQueryError("observation_not_found")
        return self._project_observation(tenant.tenant_reference, stored)

    def list_posting_receipt_observations(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> PostingReceiptObservationPresentmentPage:
        """Return one tenant page of observation summaries without calling AIS.

        Order is ``observed_at`` then ``posting_receipt_observation_id``.
        The envelope is ``posting_receipt_observations`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_posting_receipt_observations(tenant.tenant_account_id),
            key=lambda observation: (
                _parse_observed_at(observation.observed_at),
                observation.posting_receipt_observation_id,
            ),
        )
        matched: list[StoredPostingReceiptObservation] = []
        for stored in stored_rows:
            observed_at = _parse_observed_at(stored.observed_at)
            if cursor_key is not None and (
                observed_at,
                stored.posting_receipt_observation_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(
                _parse_observed_at(last.observed_at),
                last.posting_receipt_observation_id,
            )
        return PostingReceiptObservationPresentmentPage(
            posting_receipt_observations=tuple(
                self._project_observation(tenant.tenant_reference, stored)
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise PostingReceiptObservationPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_observation(
        self, tenant_reference: str, stored: StoredPostingReceiptObservation
    ) -> PostingReceiptObservationPresentmentResult:
        """Project one stored observation using only persisted commercial fields."""
        return PostingReceiptObservationPresentmentResult(
            posting_receipt_observation_id=stored.posting_receipt_observation_id,
            tenant_reference=tenant_reference,
            source_proposal_id=stored.source_proposal_id,
            idempotency_key=stored.idempotency_key,
            receipt_id=stored.receipt_id,
            receipt_contract_version=stored.receipt_contract_version,
            source_payload_hash=stored.source_payload_hash,
            posting_status_code=stored.posting_status_code,
            recorded_at=stored.recorded_at,
            observed_at=_parse_observed_at(stored.observed_at),
            posted_at=stored.posted_at,
            next_operator_action=next_operator_action(),
        )


def _format_observed_at(observed_at: datetime) -> str:
    """Render an observation timestamp as a timezone-aware ISO 8601 instant."""
    return observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_observed_at(observed_at: str) -> datetime:
    """Parse a stored observation timestamp into a timezone-aware instant."""
    return parse_iso8601_datetime(observed_at)


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise PostingReceiptObservationPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise PostingReceiptObservationPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise PostingReceiptObservationPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(observed_at: datetime, observation_id: UUID) -> str:
    """Encode the keyset cursor as observed_at then observation id."""
    return f"{_format_observed_at(observed_at)}|{observation_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        observed_text, observation_text = cursor.split("|", 1)
        return parse_iso8601_datetime(observed_text), UUID(observation_text)
    except (TypeError, ValueError) as error:
        raise PostingReceiptObservationPresentmentQueryError("request_invalid") from error
