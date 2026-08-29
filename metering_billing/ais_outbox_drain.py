"""Drain AIS posting-receipt outbox events into Billing observations.

The buyer-facing path is:

1. GET ``{AIS_BASE_URL}/outbox-events?event_type_code=posting_receipt``.
2. If ``outbox_events`` is empty, write zero receipt GETs.
3. Match each row by equality to URNs constructed from our ``proposal_id``.
4. Pull ``GET /posting-receipts?idempotency_key=`` with the stored Billing key.
5. POST ``/outbox-events/{outbox_event_id}/publish`` after a stored observation.
6. Optionally repeat the tenant-scoped drain on a stop-event-aware interval.

``payload_reference`` is never parsed and is never used as the receipt query
(Fielding et al., 2022).  ``journal_reversal`` and ``period_close`` are not
drained.  ``proposal_status`` stays ``validated``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Iterable
import math
from threading import Event
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    AisOutboxDrainOutcomeCode,
    AisOutboxDrainRejectionReasonCode,
    PostingReceiptObservationOutcomeCode,
)
from metering_billing.posting_receipt import (
    AisOutboxEvent,
    AisPostingReceiptClient,
    AisTransportError,
    PostingReceiptPullService,
    ais_base_url_is_allowed,
    general_journal_aggregate_reference,
    posting_receipt_payload_reference,
)
from metering_billing.usage_ledger import MemoryUsageLedger, StoredJournalProposal


Clock = Callable[[], datetime]
AIS_OUTBOX_DRAIN_CONTRACT_VERSION = 1
POSTING_RECEIPT_EVENT_TYPE = "posting_receipt"
DEFAULT_OUTBOX_PAGE_LIMIT = 50
SUCCESS_OBSERVATION_OUTCOMES = frozenset(
    {
        PostingReceiptObservationOutcomeCode.ACCEPTED,
        PostingReceiptObservationOutcomeCode.DUPLICATE_REPLAY,
    }
)


@dataclass(frozen=True)
class AisOutboxDrainResult:
    """Buyer-facing result of one explicit AIS outbox drain."""

    ais_outbox_drain_outcome_code: AisOutboxDrainOutcomeCode
    ais_outbox_drain_contract_version: int
    outbox_event_count: int
    receipt_lookup_count: int
    observed_receipt_count: int
    published_event_count: int
    skipped_event_count: int
    next_cursor: str | None
    rejection_reason_code: AisOutboxDrainRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return drain counts, or a sparse rejected operational result."""
        outcome = self.ais_outbox_drain_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, AisOutboxDrainOutcomeCode) else str(outcome)
        )
        payload: dict[str, object] = {
            "ais_outbox_drain_contract_version": self.ais_outbox_drain_contract_version,
            "ais_outbox_drain_outcome_code": outcome_text,
        }
        if outcome_text == AisOutboxDrainOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND.value
            )
            return payload
        if outcome_text != AisOutboxDrainOutcomeCode.ACCEPTED:
            raise ValueError(f"unsupported ais outbox drain outcome: {outcome_text}")
        payload["outbox_event_count"] = self.outbox_event_count
        payload["receipt_lookup_count"] = self.receipt_lookup_count
        payload["observed_receipt_count"] = self.observed_receipt_count
        payload["published_event_count"] = self.published_event_count
        payload["skipped_event_count"] = self.skipped_event_count
        payload["next_cursor"] = self.next_cursor
        return payload


class AisOutboxDrainService:
    """Run one tenant-scoped AIS outbox drain."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        ais_client: AisPostingReceiptClient | None = None,
        clock: Clock | None = None,
        pull_service: PostingReceiptPullService | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self.ais_client = ais_client
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self.pulls = (
            pull_service
            if pull_service is not None
            else PostingReceiptPullService(
                self.ledger, ais_client=ais_client, clock=self._clock
            )
        )

    def drain_ais_outbox(self, tenant_reference: str) -> AisOutboxDrainResult:
        """Drain ``posting_receipt`` outbox rows for one pinned tenant.

        Empty ``outbox_events`` is success and performs zero receipt GETs.
        Matched rows use the stored Billing idempotency key, never the payload
        URN, as the ``GET /posting-receipts`` query.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None or tenant is None:
            return _rejected(AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND)
        if self.ais_client is None:
            return _rejected(AisOutboxDrainRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED)
        if isinstance(self.ais_client, AisPostingReceiptClient) and not ais_base_url_is_allowed(
            self.ais_client.ais_base_url
        ):
            return _rejected(AisOutboxDrainRejectionReasonCode.AIS_BASE_URL_INSECURE)
        proposals = {
            (
                posting_receipt_payload_reference(proposal.journal_proposal_id),
                general_journal_aggregate_reference(proposal.journal_proposal_id),
            ): proposal
            for proposal in self.ledger.list_journal_proposals(tenant.tenant_account_id)
        }
        outbox_event_count = 0
        receipt_lookup_count = 0
        observed_receipt_count = 0
        published_event_count = 0
        skipped_event_count = 0
        next_cursor: str | None = None
        cursor: str | None = None
        while True:
            try:
                page = self.ais_client.list_outbox_events(
                    tenant_reference,
                    event_type_code=POSTING_RECEIPT_EVENT_TYPE,
                    page_limit=DEFAULT_OUTBOX_PAGE_LIMIT,
                    cursor=cursor,
                )
            except AisTransportError:
                return _rejected(AisOutboxDrainRejectionReasonCode.TRANSPORT_FAILURE)
            except AttributeError:
                return _rejected(AisOutboxDrainRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED)
            if page.status_code == 403:
                return _rejected(AisOutboxDrainRejectionReasonCode.CROSS_TENANT)
            if page.status_code == 404:
                return _rejected(AisOutboxDrainRejectionReasonCode.AIS_OUTBOX_INVALID)
            if page.status_code != 200:
                return _rejected(AisOutboxDrainRejectionReasonCode.TRANSPORT_FAILURE)
            next_cursor = page.next_cursor
            if not page.outbox_events:
                break
            for event in page.outbox_events:
                outbox_event_count += 1
                lookup_delta, observed_delta, published_delta, skipped_delta = self._drain_event(
                    tenant_reference, tenant.tenant_account_id, event, proposals
                )
                receipt_lookup_count += lookup_delta
                observed_receipt_count += observed_delta
                published_event_count += published_delta
                skipped_event_count += skipped_delta
            if next_cursor is None:
                break
            cursor = next_cursor
        return AisOutboxDrainResult(
            ais_outbox_drain_outcome_code=AisOutboxDrainOutcomeCode.ACCEPTED,
            ais_outbox_drain_contract_version=AIS_OUTBOX_DRAIN_CONTRACT_VERSION,
            outbox_event_count=outbox_event_count,
            receipt_lookup_count=receipt_lookup_count,
            observed_receipt_count=observed_receipt_count,
            published_event_count=published_event_count,
            skipped_event_count=skipped_event_count,
            next_cursor=next_cursor,
            rejection_reason_code=None,
        )

    def _drain_event(
        self,
        tenant_reference: str,
        tenant_account_id: UUID,
        event: AisOutboxEvent,
        proposals: dict[tuple[str, str], StoredJournalProposal],
    ) -> tuple[int, int, int, int]:
        """Match one outbox row and optionally pull then publish."""
        if event.event_type_code != POSTING_RECEIPT_EVENT_TYPE:
            return 0, 0, 0, 1
        proposal = proposals.get((event.payload_reference, event.aggregate_reference))
        if proposal is None:
            return 0, 0, 0, 1
        existing = self.ledger.find_posting_receipt_observation(
            tenant_account_id, proposal.idempotency_key
        )
        receipt_lookups = 0
        if existing is None:
            pulled = self.pulls.pull_posting_receipt(tenant_reference, proposal.idempotency_key)
            receipt_lookups = 1
            if pulled.posting_receipt_observation_outcome_code not in SUCCESS_OBSERVATION_OUTCOMES:
                return receipt_lookups, 0, 0, 1
        if not self._publish(tenant_reference, event.outbox_event_id):
            return receipt_lookups, 1, 0, 1
        return receipt_lookups, 1, 1, 0

    def _publish(self, tenant_reference: str, outbox_event_id: str) -> bool:
        """POST publish for one drained id.  403/404 do not invent a row."""
        try:
            published = self.ais_client.publish_outbox_event(tenant_reference, outbox_event_id)
        except AisTransportError:
            return False
        except AttributeError:
            return False
        return published.status_code in {200, 204}


class AisOutboxScheduler:
    """Periodically run tenant-scoped drains until cooperative shutdown."""

    def __init__(
        self,
        drain_service: AisOutboxDrainService,
        tenant_references: Callable[[], Iterable[str]],
        *,
        interval_seconds: int | float = 60,
        stop_event: Event | None = None,
        on_cycle: Callable[[tuple[tuple[str, AisOutboxDrainResult], ...]], None] | None = None,
    ) -> None:
        """Bind a dynamic tenant source, interval, and optional result observer."""
        if type(interval_seconds) not in (int, float):
            raise ValueError("interval_seconds must be a positive number")
        try:
            normalized_interval = float(interval_seconds)
        except OverflowError as error:
            raise ValueError("interval_seconds must be a positive number") from error
        if not math.isfinite(normalized_interval) or normalized_interval <= 0:
            raise ValueError("interval_seconds must be a positive number")
        self._drain_service = drain_service
        self._tenant_references = tenant_references
        self._interval_seconds = normalized_interval
        self._stop_event = stop_event if stop_event is not None else Event()
        self._on_cycle = on_cycle

    def run_once(self) -> tuple[tuple[str, AisOutboxDrainResult], ...]:
        """Drain each tenant currently returned by the configured tenant source."""
        return tuple(
            (tenant_reference, self._drain_service.drain_ais_outbox(tenant_reference))
            for tenant_reference in self._tenant_references()
        )

    def run_forever(self) -> None:
        """Run drains until ``stop_event`` is set, waking promptly for shutdown."""
        while not self._stop_event.is_set():
            cycle_results = self.run_once()
            if self._on_cycle is not None:
                self._on_cycle(cycle_results)
            self._stop_event.wait(self._interval_seconds)


def _rejected(reason: AisOutboxDrainRejectionReasonCode) -> AisOutboxDrainResult:
    """Return a sparse rejected drain result."""
    return AisOutboxDrainResult(
        ais_outbox_drain_outcome_code=AisOutboxDrainOutcomeCode.REJECTED,
        ais_outbox_drain_contract_version=AIS_OUTBOX_DRAIN_CONTRACT_VERSION,
        outbox_event_count=0,
        receipt_lookup_count=0,
        observed_receipt_count=0,
        published_event_count=0,
        skipped_event_count=0,
        next_cursor=None,
        rejection_reason_code=reason,
    )
