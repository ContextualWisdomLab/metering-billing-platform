"""Pull AIS posting receipts and store them as commercial observations.

The service is the buyer-facing observation path:

1. Require a tenant and an AIS idempotency key.
2. GET ``{ais_base_url}/posting-receipts?idempotency_key=`` with
   ``X-CWL-Tenant-Reference``.
3. Validate the AIS-owned contract.  Do not claim Billing owns it.
4. Persist one append-only ``posting_receipt_observation``.
5. Leave the source journal ``proposal_status`` unchanged.

``posting_status_code`` is an AIS fact.  A successful accept of a validated
proposal returns ``posted``.  ``held``, ``rejected``, and ``reversed`` are also
AIS outcomes.  None of them map onto Billing ``proposal_status``
(Fielding et al., 2022; International Organization for Standardization, 2026).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from metering_billing.contracts import validate_consumed_posting_receipt
from metering_billing.errors import (
    PostingReceiptObservationOutcomeCode,
    PostingReceiptObservationQueryError,
    PostingReceiptObservationRejectionReasonCode,
)
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredPostingReceiptObservation,
    generate_record_id,
)


Clock = Callable[[], datetime]
UrlOpen = Callable[..., Any]


class AisTransportError(Exception):
    """Raised when the AIS lookup cannot be completed safely."""


@dataclass(frozen=True)
class AisLookupResult:
    """Raw HTTP result from AIS posting-receipt lookup."""

    status_code: int
    raw_body: bytes


@dataclass(frozen=True)
class AisOutboxEvent:
    """One AIS outbox row.  References are opaque strings until Billing matches them."""

    outbox_event_id: str
    event_type_code: str
    aggregate_reference: str
    payload_reference: str
    payload_hash: str
    created_at: str


@dataclass(frozen=True)
class AisOutboxPage:
    """One AIS ``GET /outbox-events`` page.  Never uses body ``items`` or ``cursor``."""

    status_code: int
    outbox_events: tuple[AisOutboxEvent, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class PostingReceiptObservationResult:
    """Buyer-facing result of pulling or reading one posting-receipt observation."""

    posting_receipt_observation_outcome_code: PostingReceiptObservationOutcomeCode
    posting_receipt_observation_id: UUID | None
    receipt_id: UUID | None
    receipt_contract_version: int
    idempotency_key: str | None
    source_proposal_id: UUID | None
    source_payload_hash: str | None
    tenant_reference: str | None
    legal_entity_reference: str | None
    accounting_book_reference: str | None
    accounting_policy_version: str | None
    posting_rule_version: str | None
    posting_status_code: str | None
    recorded_at: str | None
    fiscal_period_reference: str | None
    journal_reference: str | None
    reversal_of_journal_reference: str | None
    hold_reason_code: str | None
    receipt_rejection_reason_code: str | None
    posted_at: str | None
    line_count: int | None
    transaction_currency: str | None
    functional_currency: str | None
    observed_at: datetime | None
    rejection_reason_code: PostingReceiptObservationRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the stored observation, or a sparse rejected operational result."""
        outcome = self.posting_receipt_observation_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, PostingReceiptObservationOutcomeCode)
            else str(outcome)
        )
        if outcome_text == PostingReceiptObservationOutcomeCode.REJECTED:
            reason = self.rejection_reason_code
            return {
                "posting_receipt_observation_outcome_code": outcome_text,
                "rejection_reason_code": (
                    reason.value
                    if reason is not None
                    else PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID.value
                ),
            }
        if (
            outcome_text != PostingReceiptObservationOutcomeCode.ACCEPTED
            and outcome_text != PostingReceiptObservationOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported posting receipt observation outcome: {outcome_text}")
        if self.posting_receipt_observation_id is None or self.observed_at is None:
            raise ValueError("accepted observations must include identity and observed_at")
        payload: dict[str, object] = {
            "posting_receipt_observation_outcome_code": outcome_text,
            "posting_receipt_observation_id": str(self.posting_receipt_observation_id),
            "receipt_id": str(self.receipt_id),
            "receipt_contract_version": self.receipt_contract_version,
            "idempotency_key": self.idempotency_key,
            "source_proposal_id": str(self.source_proposal_id),
            "source_payload_hash": self.source_payload_hash,
            "tenant_reference": self.tenant_reference,
            "legal_entity_reference": self.legal_entity_reference,
            "accounting_book_reference": self.accounting_book_reference,
            "accounting_policy_version": self.accounting_policy_version,
            "posting_rule_version": self.posting_rule_version,
            "posting_status_code": self.posting_status_code,
            "recorded_at": self.recorded_at,
            "observed_at": _format_observed_at(self.observed_at),
        }
        optional_values = {
            "fiscal_period_reference": self.fiscal_period_reference,
            "journal_reference": self.journal_reference,
            "reversal_of_journal_reference": self.reversal_of_journal_reference,
            "hold_reason_code": self.hold_reason_code,
            "rejection_reason_code": self.receipt_rejection_reason_code,
            "posted_at": self.posted_at,
            "line_count": self.line_count,
            "transaction_currency": self.transaction_currency,
            "functional_currency": self.functional_currency,
        }
        for field_name, field_value in optional_values.items():
            if field_value is not None:
                payload[field_name] = field_value
        return payload


def ais_base_url_is_allowed(ais_base_url: str) -> bool:
    """Return whether *ais_base_url* is https, or http on a local test host.

    ``file://`` and non-local http origins are refused.  The drain never takes
    a host from the caller body; ``AIS_BASE_URL`` is operator-configured.
    """
    if not isinstance(ais_base_url, str) or not ais_base_url:
        return False
    parsed = urlparse(ais_base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def posting_receipt_payload_reference(proposal_id: UUID) -> str:
    """Return the AIS-pinned payload URN for one Billing ``proposal_id``."""
    return f"urn:cwl:accounting:posting_receipt:{proposal_id}"


def general_journal_aggregate_reference(proposal_id: UUID) -> str:
    """Return the AIS-pinned aggregate URN for one Billing ``proposal_id``."""
    return f"urn:cwl:accounting:general_journal:{proposal_id}"


class AisPostingReceiptClient:
    """Stdlib HTTP client for AIS posting-receipt and outbox routes."""

    def __init__(
        self,
        ais_base_url: str,
        urlopen: UrlOpen | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.ais_base_url = ais_base_url.rstrip("/")
        self._urlopen: UrlOpen = urlopen if urlopen is not None else urlopen_default
        self.timeout_seconds = timeout_seconds

    def get_posting_receipt(self, tenant_reference: str, idempotency_key: str) -> AisLookupResult:
        """GET one posting receipt.  The tenant pin header is always sent."""
        query = urlencode({"idempotency_key": idempotency_key})
        request = Request(
            f"{self.ais_base_url}/posting-receipts?{query}",
            method="GET",
            headers={
                "X-CWL-Tenant-Reference": tenant_reference,
                "Accept": "application/json",
            },
        )
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw_body = response.read()
        except HTTPError as error:
            if error.code in {403, 404}:
                return AisLookupResult(status_code=error.code, raw_body=b"")
            raise AisTransportError("transport_failure") from error
        except (URLError, TimeoutError, OSError) as error:
            raise AisTransportError("transport_failure") from error
        if status_code != 200:
            raise AisTransportError("transport_failure")
        return AisLookupResult(status_code=200, raw_body=raw_body)

    def list_outbox_events(
        self,
        tenant_reference: str,
        event_type_code: str = "posting_receipt",
        page_limit: int = 50,
        cursor: str | None = None,
    ) -> AisOutboxPage:
        """GET unpublished AIS outbox events.  Query ``event_type_code`` is required.

        The client reads ``outbox_events`` and ``next_cursor`` only.  It never
        reads body ``items`` or body ``cursor``.
        """
        if event_type_code != "posting_receipt":
            raise AisTransportError("transport_failure")
        if not isinstance(page_limit, int) or isinstance(page_limit, bool):
            raise AisTransportError("transport_failure")
        if page_limit < 1 or page_limit > 100:
            raise AisTransportError("transport_failure")
        query_fields: dict[str, str] = {
            "event_type_code": event_type_code,
            "page_limit": str(page_limit),
        }
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise AisTransportError("transport_failure")
            query_fields["cursor"] = cursor
        query = urlencode(query_fields)
        request = Request(
            f"{self.ais_base_url}/outbox-events?{query}",
            method="GET",
            headers={
                "X-CWL-Tenant-Reference": tenant_reference,
                "Accept": "application/json",
            },
        )
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw_body = response.read()
        except HTTPError as error:
            if error.code in {403, 404}:
                return AisOutboxPage(status_code=error.code, outbox_events=(), next_cursor=None)
            raise AisTransportError("transport_failure") from error
        except (URLError, TimeoutError, OSError) as error:
            raise AisTransportError("transport_failure") from error
        if status_code != 200:
            raise AisTransportError("transport_failure")
        return _parse_outbox_page(raw_body)

    def publish_outbox_event(self, tenant_reference: str, outbox_event_id: str) -> AisLookupResult:
        """POST AIS ``/outbox-events/{id}/publish``.  GET on that path is not used."""
        if not isinstance(outbox_event_id, str) or not outbox_event_id:
            raise AisTransportError("transport_failure")
        request = Request(
            f"{self.ais_base_url}/outbox-events/{outbox_event_id}/publish",
            data=b"",
            method="POST",
            headers={
                "X-CWL-Tenant-Reference": tenant_reference,
                "Accept": "application/json",
            },
        )
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw_body = response.read()
        except HTTPError as error:
            if error.code in {403, 404}:
                return AisLookupResult(status_code=error.code, raw_body=b"")
            raise AisTransportError("transport_failure") from error
        except (URLError, TimeoutError, OSError) as error:
            raise AisTransportError("transport_failure") from error
        if status_code not in {200, 204}:
            raise AisTransportError("transport_failure")
        return AisLookupResult(status_code=status_code, raw_body=raw_body)


def _parse_outbox_page(raw_body: bytes) -> AisOutboxPage:
    """Decode one AIS outbox envelope without reading ``items`` or ``cursor``."""
    try:
        loaded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AisTransportError("transport_failure") from error
    if not isinstance(loaded, dict):
        raise AisTransportError("transport_failure")
    events = loaded.get("outbox_events")
    if not isinstance(events, list):
        raise AisTransportError("transport_failure")
    next_cursor = loaded.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise AisTransportError("transport_failure")
    parsed: list[AisOutboxEvent] = []
    for item in events:
        if not isinstance(item, dict):
            raise AisTransportError("transport_failure")
        try:
            parsed.append(
                AisOutboxEvent(
                    outbox_event_id=str(item["outbox_event_id"]),
                    event_type_code=str(item["event_type_code"]),
                    aggregate_reference=str(item["aggregate_reference"]),
                    payload_reference=str(item["payload_reference"]),
                    payload_hash=str(item["payload_hash"]),
                    created_at=str(item["created_at"]),
                )
            )
        except KeyError as error:
            raise AisTransportError("transport_failure") from error
    return AisOutboxPage(status_code=200, outbox_events=tuple(parsed), next_cursor=next_cursor)


def urlopen_default(request: Request, timeout: float | None = None) -> Any:
    """Call stdlib ``urllib.request.urlopen`` so tests can replace the client hook."""
    return urlopen(request, timeout=timeout)


class PostingReceiptPullService:
    """Append-only AIS posting-receipt observer backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        ais_client: AisPostingReceiptClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self.ais_client = ais_client
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def pull_posting_receipt(
        self, tenant_reference: str, idempotency_key: str
    ) -> PostingReceiptObservationResult:
        """Pull one AIS receipt and store it as a commercial observation.

        Replay of the same tenant, key, receipt, and payload hash returns the
        stored observation as ``duplicate_replay``.  A conflicting receipt for
        the same key fails closed.  The source journal ``proposal_status`` is
        never updated.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(PostingReceiptObservationRejectionReasonCode.TENANT_NOT_FOUND)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return _rejected(PostingReceiptObservationRejectionReasonCode.IDEMPOTENCY_KEY_MISSING)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(PostingReceiptObservationRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if self.ais_client is None:
            return _rejected(PostingReceiptObservationRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED)
        try:
            lookup = self.ais_client.get_posting_receipt(tenant_reference, idempotency_key)
        except AisTransportError:
            return _rejected(PostingReceiptObservationRejectionReasonCode.TRANSPORT_FAILURE)
        if lookup.status_code == 403:
            return _rejected(PostingReceiptObservationRejectionReasonCode.CROSS_TENANT)
        if lookup.status_code == 404:
            return _rejected(PostingReceiptObservationRejectionReasonCode.NOT_YET_ACCEPTED)
        if lookup.status_code != 200:
            return _rejected(PostingReceiptObservationRejectionReasonCode.TRANSPORT_FAILURE)
        try:
            receipt = _parse_ais_receipt(lookup.raw_body)
        except ValueError:
            return _rejected(PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID)
        if receipt["tenant_reference"] != tenant_reference:
            return _rejected(PostingReceiptObservationRejectionReasonCode.TENANT_MISMATCH)
        if receipt["idempotency_key"] != idempotency_key:
            return _rejected(PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID)
        receipt_id = UUID(str(receipt["receipt_id"]))
        source_payload_hash = str(receipt["source_payload_hash"])
        existing = self.ledger.find_posting_receipt_observation(
            tenant.tenant_account_id, idempotency_key
        )
        if existing is not None:
            if (
                existing.receipt_id == receipt_id
                and existing.source_payload_hash == source_payload_hash
            ):
                return _from_stored(
                    existing,
                    tenant_reference,
                    PostingReceiptObservationOutcomeCode.DUPLICATE_REPLAY,
                )
            return _rejected(PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT)
        existing_receipt = self.ledger.find_posting_receipt_observation_by_receipt(
            tenant.tenant_account_id, receipt_id
        )
        if existing_receipt is not None:
            return _rejected(PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT)
        observed_at = self._clock()
        observation = StoredPostingReceiptObservation(
            posting_receipt_observation_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            receipt_id=receipt_id,
            receipt_contract_version=int(receipt["receipt_contract_version"]),
            idempotency_key=idempotency_key,
            source_proposal_id=UUID(str(receipt["source_proposal_id"])),
            source_payload_hash=source_payload_hash,
            legal_entity_reference=str(receipt["legal_entity_reference"]),
            accounting_book_reference=str(receipt["accounting_book_reference"]),
            accounting_policy_version=str(receipt["accounting_policy_version"]),
            posting_rule_version=str(receipt["posting_rule_version"]),
            posting_status_code=str(receipt["posting_status_code"]),
            recorded_at=str(receipt["recorded_at"]),
            fiscal_period_reference=_optional_str(receipt, "fiscal_period_reference"),
            journal_reference=_optional_str(receipt, "journal_reference"),
            reversal_of_journal_reference=_optional_str(receipt, "reversal_of_journal_reference"),
            hold_reason_code=_optional_str(receipt, "hold_reason_code"),
            rejection_reason_code=_optional_str(receipt, "rejection_reason_code"),
            posted_at=_optional_str(receipt, "posted_at"),
            line_count=_optional_int(receipt, "line_count"),
            transaction_currency=_optional_str(receipt, "transaction_currency"),
            functional_currency=_optional_str(receipt, "functional_currency"),
            observed_at=_format_observed_at(observed_at),
        )
        try:
            stored = self.ledger.insert_posting_receipt_observation(observation)
        except ValueError:
            return _rejected(PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT)
        return _from_stored(stored, tenant_reference, PostingReceiptObservationOutcomeCode.ACCEPTED)

    def get_posting_receipt_observation(
        self, tenant_reference: str, idempotency_key: str
    ) -> PostingReceiptObservationResult:
        """Return one same-tenant stored observation without calling AIS.

        A missing or cross-tenant key is indistinguishable.  The read does not
        change ``proposal_status``.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise PostingReceiptObservationQueryError("tenant_not_found")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise PostingReceiptObservationQueryError("idempotency_key_missing")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise PostingReceiptObservationQueryError("tenant_not_found")
        assert tenant is not None
        stored = self.ledger.find_posting_receipt_observation(
            tenant.tenant_account_id, idempotency_key
        )
        if stored is None:
            raise PostingReceiptObservationQueryError("observation_not_found")
        return _from_stored(stored, tenant_reference, PostingReceiptObservationOutcomeCode.ACCEPTED)


def _parse_ais_receipt(raw_body: bytes) -> dict[str, Any]:
    """Decode and validate one AIS posting-receipt object."""
    try:
        loaded = json.loads(raw_body.decode("utf-8"))
        _reject_binary_floats(loaded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("receipt_invalid") from error
    if not isinstance(loaded, dict):
        raise ValueError("receipt_invalid")
    errors = validate_consumed_posting_receipt(loaded)
    if errors:
        raise ValueError("receipt_invalid")
    return loaded


def _reject_binary_floats(value: Any) -> None:
    """Fail closed when a decoded JSON tree still contains a binary float."""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise ValueError("receipt_invalid")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_binary_floats(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_binary_floats(item)


def _optional_str(payload: Mapping[str, Any], field_name: str) -> str | None:
    """Return an optional string field, or ``None`` when absent."""
    value = payload.get(field_name)
    if value is None:
        return None
    return str(value)


def _optional_int(payload: Mapping[str, Any], field_name: str) -> int | None:
    """Return an optional integer field, or ``None`` when absent."""
    value = payload.get(field_name)
    if value is None:
        return None
    return int(value)


def _rejected(
    reason: PostingReceiptObservationRejectionReasonCode,
) -> PostingReceiptObservationResult:
    """Return a sparse rejected observation result."""
    return PostingReceiptObservationResult(
        posting_receipt_observation_outcome_code=PostingReceiptObservationOutcomeCode.REJECTED,
        posting_receipt_observation_id=None,
        receipt_id=None,
        receipt_contract_version=1,
        idempotency_key=None,
        source_proposal_id=None,
        source_payload_hash=None,
        tenant_reference=None,
        legal_entity_reference=None,
        accounting_book_reference=None,
        accounting_policy_version=None,
        posting_rule_version=None,
        posting_status_code=None,
        recorded_at=None,
        fiscal_period_reference=None,
        journal_reference=None,
        reversal_of_journal_reference=None,
        hold_reason_code=None,
        receipt_rejection_reason_code=None,
        posted_at=None,
        line_count=None,
        transaction_currency=None,
        functional_currency=None,
        observed_at=None,
        rejection_reason_code=reason,
    )


def _from_stored(
    stored: StoredPostingReceiptObservation,
    tenant_reference: str,
    outcome: PostingReceiptObservationOutcomeCode,
) -> PostingReceiptObservationResult:
    """Project a persisted observation into the buyer-facing result."""
    observed_stamp = datetime.fromisoformat(stored.observed_at.replace("Z", "+00:00"))
    return PostingReceiptObservationResult(
        posting_receipt_observation_outcome_code=outcome,
        posting_receipt_observation_id=stored.posting_receipt_observation_id,
        receipt_id=stored.receipt_id,
        receipt_contract_version=stored.receipt_contract_version,
        idempotency_key=stored.idempotency_key,
        source_proposal_id=stored.source_proposal_id,
        source_payload_hash=stored.source_payload_hash,
        tenant_reference=tenant_reference,
        legal_entity_reference=stored.legal_entity_reference,
        accounting_book_reference=stored.accounting_book_reference,
        accounting_policy_version=stored.accounting_policy_version,
        posting_rule_version=stored.posting_rule_version,
        posting_status_code=stored.posting_status_code,
        recorded_at=stored.recorded_at,
        fiscal_period_reference=stored.fiscal_period_reference,
        journal_reference=stored.journal_reference,
        reversal_of_journal_reference=stored.reversal_of_journal_reference,
        hold_reason_code=stored.hold_reason_code,
        receipt_rejection_reason_code=stored.rejection_reason_code,
        posted_at=stored.posted_at,
        line_count=stored.line_count,
        transaction_currency=stored.transaction_currency,
        functional_currency=stored.functional_currency,
        observed_at=observed_stamp,
        rejection_reason_code=None,
    )


def _format_observed_at(observed_at: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for the observation instant."""
    return observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
