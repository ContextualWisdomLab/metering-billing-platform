"""Immutable usage ingestion with tenant isolation and idempotent deduplication.

The service is the buyer-facing write path for canonical usage events:

1. Validate the published usage-event contract.
2. Verify the source-payload hash against the current contract version.
3. Resolve tenant-scoped attribution that cannot cross tenants.
4. Accept exact decimal measurements against an effective meter and quality rule.
5. Persist a new event, acknowledge an identical replay, or reject a conflict.

The service does not rate usage, create invoices, talk to a payment provider, or
emit a posted accounting journal.  Successful ingest only writes usage facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from contextlib import nullcontext
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from metering_billing.contracts import (
    USAGE_EVENT_SCHEMA_NAME,
    load_json_schema,
    validate_schema_instance,
    validate_usage_event,
)
from metering_billing.errors import (
    ExactDecimalError,
    IngestionOutcomeCode,
    RejectionReasonCode,
    TimeWindowError,
    UsageEventConflict,
    require_resolved,
)
from metering_billing.exact_decimal import parse_exact_decimal
from metering_billing.payload_integrity import source_payload_hash_errors
from metering_billing.time_window import TimeWindow, parse_iso8601_datetime
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredIngestionReceipt,
    StoredUsageEvent,
    StoredUsageMeasurement,
    generate_record_id,
)
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class EventIngestionReceipt:
    """Per-event result that a producer can persist or retry against."""

    source_event_key: str
    event_contract_version: int | None
    source_payload_hash: str | None
    tenant_reference: str | None
    ingestion_outcome_code: IngestionOutcomeCode
    usage_event_id: UUID | None
    rejection_reason_code: RejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, Any]:
        """Return the closed JSON object published in the receipt schema."""
        payload: dict[str, Any] = {
            "source_event_key": self.source_event_key,
            "ingestion_outcome_code": self.ingestion_outcome_code.value,
        }
        if self.event_contract_version is not None:
            payload["event_contract_version"] = self.event_contract_version
        if self.source_payload_hash is not None:
            payload["source_payload_hash"] = self.source_payload_hash
        if self.tenant_reference is not None:
            payload["tenant_reference"] = self.tenant_reference
        if self.usage_event_id is not None:
            payload["usage_event_id"] = str(self.usage_event_id)
        if self.rejection_reason_code is not None:
            payload["rejection_reason_code"] = self.rejection_reason_code.value
        return payload


@dataclass(frozen=True)
class BatchIngestionReceipt:
    """Ordered batch result with exact accepted, replay, and rejected counts."""

    batch_receipt_id: UUID
    receipt_contract_version: int
    accepted_event_count: int
    duplicate_replay_count: int
    rejected_event_count: int
    event_receipts: tuple[EventIngestionReceipt, ...]

    def as_contract_dict(self) -> dict[str, Any]:
        """Return the closed JSON object published in the receipt schema."""
        return {
            "batch_receipt_id": str(self.batch_receipt_id),
            "receipt_contract_version": self.receipt_contract_version,
            "accepted_event_count": self.accepted_event_count,
            "duplicate_replay_count": self.duplicate_replay_count,
            "rejected_event_count": self.rejected_event_count,
            "event_receipts": [receipt.as_contract_dict() for receipt in self.event_receipts],
        }


class UsageIngestionService:
    """Append-only usage writer backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._usage_event_schema = load_json_schema(USAGE_EVENT_SCHEMA_NAME)

    def ingest_usage_event(
        self,
        event: Any,
        time_window: TimeWindow | None = None,
    ) -> EventIngestionReceipt:
        """Ingest one event.  Replays of the same fact return the stored row."""
        transaction = getattr(self.ledger, "ingestion_transaction", None)
        context = nullcontext() if transaction is None else transaction()
        with context:
            try:
                receipt = self._decide_usage_event(event, time_window)
            except UsageEventConflict as conflict:
                if conflict.duplicate_replay:
                    receipt = _replay(event, conflict.existing)
                else:
                    receipt = _rejected(
                        *_extract_identity(event),
                        conflict.rejection_reason_code
                        or RejectionReasonCode.SOURCE_EVENT_CONFLICT,
                    )
            self._persist_ingestion_receipt(receipt)
        return receipt

    def _decide_usage_event(
        self,
        event: Any,
        time_window: TimeWindow | None,
    ) -> EventIngestionReceipt:
        """Decide accept, replay, or reject without writing an audit receipt."""
        source_event_key, event_contract_version, source_payload_hash, tenant_reference = (
            _extract_identity(event)
        )
        if not isinstance(event, dict):
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.SCHEMA_INVALID,
            )

        schema_errors = validate_schema_instance(self._usage_event_schema, event)
        if schema_errors:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.SCHEMA_INVALID,
            )

        if source_payload_hash_errors(event):
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.PAYLOAD_HASH_MISMATCH,
            )

        try:
            occurred_at = parse_iso8601_datetime(event["occurred_at"])
        except TimeWindowError:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.SCHEMA_INVALID,
            )
        recorded_at = self._clock()

        if time_window is not None and not time_window.contains(occurred_at):
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.EVENT_OUTSIDE_TIME_WINDOW,
            )

        tenant, tenant_error = self.ledger.resolve_tenant(event["tenant_reference"])
        if tenant_error is not None:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                tenant_error,
            )
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_by_source_event_key(
            tenant.tenant_account_id, event["source_event_key"]
        )
        if existing is not None:
            if (
                existing.event_payload_hash == event["source_payload_hash"]
                and existing.event_contract_version == event["event_contract_version"]
            ):
                return _replay(event, existing)
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.SOURCE_EVENT_CONFLICT,
            )

        hash_existing = self.ledger.find_by_payload_hash(
            tenant.tenant_account_id,
            event["source_payload_hash"],
            event["event_contract_version"],
        )
        if hash_existing is not None:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.PAYLOAD_HASH_CONFLICT,
            )

        producer_event_id = UUID(event["event_id"])
        producer_existing = self.ledger.find_by_producer_event_id(
            tenant.tenant_account_id, producer_event_id
        )
        if producer_existing is not None:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                RejectionReasonCode.PRODUCER_EVENT_CONFLICT,
            )

        account, account_error = self.ledger.resolve_billing_account(
            tenant, event["billing_account_reference"]
        )
        if account_error is not None:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                account_error,
            )
        account = require_resolved(account, "billing_account")

        principal, principal_error = self.ledger.resolve_billing_principal(
            tenant, event["billing_principal_reference"], occurred_at
        )
        if principal_error is not None:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                principal_error,
            )
        principal = require_resolved(principal, "billing_principal")

        credential_record_id = None
        if "credential_reference" in event:
            credential, credential_error = self.ledger.resolve_credential(
                tenant,
                event["credential_reference"],
                principal,
                account,
                occurred_at,
            )
            if credential_error is not None:
                return _rejected(
                    source_event_key,
                    event_contract_version,
                    source_payload_hash,
                    tenant_reference,
                    credential_error,
                )
            credential = require_resolved(credential, "credential")
            credential_record_id = credential.credential_record_id

        usage_event_id = generate_record_id()
        try:
            measurements = self._build_measurements(usage_event_id, event["measurements"], occurred_at)
        except _MeasurementRejected as error:
            return _rejected(
                source_event_key,
                event_contract_version,
                source_payload_hash,
                tenant_reference,
                error.reason_code,
            )

        stored = self.ledger.insert_usage_event(
            StoredUsageEvent(
                usage_event_id=usage_event_id,
                producer_event_id=producer_event_id,
                tenant_account_id=tenant.tenant_account_id,
                billing_account_id=account.billing_account_id,
                billing_principal_id=principal.billing_principal_id,
                credential_record_id=credential_record_id,
                source_event_key=event["source_event_key"],
                event_contract_version=event["event_contract_version"],
                event_payload_hash=event["source_payload_hash"],
                product_code=event["product_code"],
                operation_code=event.get("operation_code"),
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                cost_center_reference=event.get("cost_center_reference"),
                project_reference=event.get("project_reference"),
                measurements=measurements,
                dimensions=tuple(sorted(event.get("dimensions", {}).items())),
            )
        )
        return EventIngestionReceipt(
            source_event_key=event["source_event_key"],
            event_contract_version=event["event_contract_version"],
            source_payload_hash=event["source_payload_hash"],
            tenant_reference=event["tenant_reference"],
            ingestion_outcome_code=IngestionOutcomeCode.ACCEPTED,
            usage_event_id=stored.usage_event_id,
            rejection_reason_code=None,
        )

    def ingest_usage_batch(
        self,
        events: Sequence[Any],
        time_window: TimeWindow | None = None,
    ) -> BatchIngestionReceipt:
        """Ingest events in order.  Each event is its own append-only transaction."""
        receipts = tuple(self.ingest_usage_event(event, time_window=time_window) for event in events)
        return BatchIngestionReceipt(
            batch_receipt_id=generate_record_id(),
            receipt_contract_version=1,
            accepted_event_count=sum(
                receipt.ingestion_outcome_code is IngestionOutcomeCode.ACCEPTED for receipt in receipts
            ),
            duplicate_replay_count=sum(
                receipt.ingestion_outcome_code is IngestionOutcomeCode.DUPLICATE_REPLAY
                for receipt in receipts
            ),
            rejected_event_count=sum(
                receipt.ingestion_outcome_code is IngestionOutcomeCode.REJECTED for receipt in receipts
            ),
            event_receipts=receipts,
        )

    def query_usage_window(
        self, tenant_reference: str, time_window: TimeWindow
    ) -> tuple[StoredUsageEvent, ...]:
        """Return stored usage for one tenant inside a half-open time window."""
        tenant, error = self.ledger.resolve_tenant(tenant_reference)
        if error is not None:
            return ()
        tenant = require_resolved(tenant, "tenant")
        return self.ledger.list_usage_events_in_window(
            tenant.tenant_account_id,
            time_window.window_started_at,
            time_window.window_ended_at,
        )

    def query_ingestion_receipts(
        self, tenant_reference: str | None = None
    ) -> tuple[StoredIngestionReceipt, ...]:
        """Return append-only ingest receipts, optionally for one tenant."""
        if tenant_reference is None:
            return self.ledger.list_ingestion_receipts()
        tenant, error = self.ledger.resolve_tenant(tenant_reference)
        if error is not None:
            return ()
        tenant = require_resolved(tenant, "tenant")
        return self.ledger.list_ingestion_receipts(tenant.tenant_account_id)

    def _persist_ingestion_receipt(self, receipt: EventIngestionReceipt) -> StoredIngestionReceipt:
        """Write the SQL-shaped audit row for one ingest attempt."""
        tenant_account_id = None
        if receipt.tenant_reference is not None:
            tenant, error = self.ledger.resolve_tenant(receipt.tenant_reference)
            if error is None:
                tenant_account_id = tenant.tenant_account_id
        return self.ledger.append_ingestion_receipt(
            StoredIngestionReceipt(
                usage_ingestion_receipt_id=generate_record_id(),
                tenant_account_id=tenant_account_id,
                usage_event_id=receipt.usage_event_id,
                source_event_key=receipt.source_event_key,
                event_contract_version=receipt.event_contract_version,
                source_payload_hash=receipt.source_payload_hash,
                ingestion_outcome_code=receipt.ingestion_outcome_code.value,
                rejection_reason_code=(
                    receipt.rejection_reason_code.value
                    if receipt.rejection_reason_code is not None
                    else None
                ),
                recorded_at=self._clock(),
            )
        )

    def _build_measurements(
        self,
        usage_event_id: UUID,
        measurements: Sequence[Mapping[str, Any]],
        occurred_at: datetime,
    ) -> tuple[StoredUsageMeasurement, ...]:
        """Normalize measurements or raise a typed rejection."""
        built: list[StoredUsageMeasurement] = []
        seen_meter_ids: set[UUID] = set()
        for measurement in measurements:
            try:
                quantity = parse_exact_decimal(measurement["quantity"])
            except ExactDecimalError as error:
                raise _MeasurementRejected(RejectionReasonCode.MEASUREMENT_QUANTITY_INVALID) from error
            definition, meter_error = self.ledger.resolve_meter(
                measurement["meter_code"],
                measurement["unit_code"],
                measurement["quality_code"],
                occurred_at,
            )
            if meter_error is not None:
                raise _MeasurementRejected(meter_error)
            definition = require_resolved(definition, "meter")
            if definition.meter_definition_id in seen_meter_ids:
                raise _MeasurementRejected(RejectionReasonCode.MEASUREMENT_METER_DUPLICATE)
            seen_meter_ids.add(definition.meter_definition_id)
            built.append(
                StoredUsageMeasurement(
                    usage_measurement_id=generate_record_id(),
                    usage_event_id=usage_event_id,
                    meter_definition_id=definition.meter_definition_id,
                    meter_code=definition.meter_code,
                    unit_code=definition.unit_code,
                    measured_quantity=quantity,
                    quality_code=measurement["quality_code"],
                )
            )
        return tuple(built)


class _MeasurementRejected(Exception):
    """Internal control-flow exception for a single failed measurement."""

    def __init__(self, reason_code: RejectionReasonCode) -> None:
        super().__init__(reason_code.value)
        self.reason_code = reason_code


def _extract_identity(event: Any) -> tuple[str, int | None, str | None, str | None]:
    """Best-effort identity fields for receipts of malformed events."""
    if not isinstance(event, dict):
        return "unavailable_source_event_key", None, None, None
    source_event_key = event.get("source_event_key")
    if not isinstance(source_event_key, str) or source_event_key == "":
        source_event_key = "unavailable_source_event_key"
    event_contract_version = event.get("event_contract_version")
    if not isinstance(event_contract_version, int):
        event_contract_version = None
    source_payload_hash = event.get("source_payload_hash")
    if not isinstance(source_payload_hash, str):
        source_payload_hash = None
    tenant_reference = event.get("tenant_reference")
    if not isinstance(tenant_reference, str):
        tenant_reference = None
    return source_event_key, event_contract_version, source_payload_hash, tenant_reference


def _rejected(
    source_event_key: str,
    event_contract_version: int | None,
    source_payload_hash: str | None,
    tenant_reference: str | None,
    reason_code: RejectionReasonCode,
) -> EventIngestionReceipt:
    """Build a rejected receipt without writing usage."""
    return EventIngestionReceipt(
        source_event_key=source_event_key,
        event_contract_version=event_contract_version,
        source_payload_hash=source_payload_hash,
        tenant_reference=tenant_reference,
        ingestion_outcome_code=IngestionOutcomeCode.REJECTED,
        usage_event_id=None,
        rejection_reason_code=reason_code,
    )


def _replay(event: Mapping[str, Any], existing: StoredUsageEvent) -> EventIngestionReceipt:
    """Acknowledge an identical replay of an already-stored event."""
    return EventIngestionReceipt(
        source_event_key=event["source_event_key"],
        event_contract_version=event["event_contract_version"],
        source_payload_hash=event["source_payload_hash"],
        tenant_reference=event["tenant_reference"],
        ingestion_outcome_code=IngestionOutcomeCode.DUPLICATE_REPLAY,
        usage_event_id=existing.usage_event_id,
        rejection_reason_code=None,
    )


def validate_event_contract(event: Any) -> tuple[str, ...]:
    """Public wrapper used by importers that only need schema validation."""
    return validate_usage_event(event)
