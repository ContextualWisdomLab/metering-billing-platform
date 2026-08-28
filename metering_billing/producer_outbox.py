"""Durable producer buffering with bounded, idempotent delivery attempts.

The outbox stores only the validated usage event and non-secret attribution
metadata.  Credentials are supplied to a transport at drain time, so rotation
does not rewrite queued facts or persist a bearer secret.  Billing remains the
monetary-effect authority: accepted and duplicate receipts are terminal only
because the server enforces the event's tenant-scoped idempotency key.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from metering_billing.contracts import validate_usage_event
from metering_billing.payload_integrity import source_payload_hash_errors
from metering_billing.producer_sdk import ProducerContractError


_DELIVERY_OUTCOMES = frozenset(
    {"accepted", "duplicate_replay", "rejected", "retryable"}
)
_OUTBOX_STATUSES = frozenset({"pending", "delivered", "dead_letter"})


class ProducerOutboxError(RuntimeError):
    """Raised when durable producer delivery configuration is invalid."""


class ProducerOutboxConflict(ProducerOutboxError):
    """Raised when one tenant key is reused for a different event fact."""


@dataclass(frozen=True)
class ProducerAuthContext:
    """Tenant and purpose context supplied to every delivery attempt."""

    tenant_reference: str
    purpose_code: str
    credential_reference: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        """Reject blank context without accepting caller-controlled secrets."""
        for field_name in ("tenant_reference", "purpose_code"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("credential_reference", "correlation_id"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be a non-empty string when provided")


@dataclass(frozen=True)
class ProducerDeliveryResult:
    """One server result keyed by the producer's stable source event key."""

    source_event_key: str
    outcome: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        """Keep transport receipts closed and actionable."""
        if not isinstance(self.source_event_key, str) or not self.source_event_key:
            raise ValueError("source_event_key must be a non-empty string")
        if self.outcome not in _DELIVERY_OUTCOMES:
            raise ValueError("unsupported producer delivery outcome")


@dataclass(frozen=True)
class ProducerEnqueueReceipt:
    """Stable result of inserting or replaying one local outbox fact."""

    outbox_event_id: str
    source_event_key: str
    duplicate_enqueue: bool


@dataclass(frozen=True)
class ProducerOutboxStatus:
    """Operator-safe state for one queued producer event."""

    outbox_event_id: str
    source_event_key: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error_code: str | None


@dataclass(frozen=True)
class ProducerDrainReceipt:
    """Partial batch receipt returned after one bounded drain attempt."""

    attempted_event_count: int
    accepted_event_count: int
    duplicate_replay_count: int
    rejected_event_count: int
    retry_scheduled_event_count: int
    dead_letter_event_count: int
    event_results: tuple[ProducerDeliveryResult, ...]


class ProducerOutboxTransport(Protocol):
    """Transport seam for the Billing batch endpoint."""

    def ingest_batch(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        auth: ProducerAuthContext,
        credential: str | None,
    ) -> Sequence[ProducerDeliveryResult]:
        """Send one bounded batch without persisting the credential."""
        ...  # pragma: no cover


class ProducerOutbox:
    """SQLite-backed producer outbox with leases and bounded exponential retry."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Any = None,
        max_attempts: int = 5,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 3600,
        lease_seconds: int = 60,
    ) -> None:
        """Open or create a durable local outbox database."""
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("max_attempts must be an integer from 1 through 32")
        if type(base_backoff_seconds) is not int or base_backoff_seconds < 1:
            raise ValueError("base_backoff_seconds must be a positive integer")
        if type(max_backoff_seconds) is not int or max_backoff_seconds < 1:
            raise ValueError("max_backoff_seconds must be a positive integer")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        # ponytail: one lock per shared SQLite connection; use a connection pool for higher throughput.
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._lease_seconds = lease_seconds
        self._initialize_schema()

    def close(self) -> None:
        """Close the local outbox connection."""
        with self._lock:
            self._connection.close()

    def enqueue(
        self, event: Mapping[str, Any], *, auth: ProducerAuthContext
    ) -> ProducerEnqueueReceipt:
        """Validate and durably enqueue one event without storing a secret."""
        with self._lock:
            if not isinstance(event, Mapping):
                raise ProducerContractError("producer outbox requires one usage-event object")
            errors = validate_usage_event(event)
            if errors:
                raise ProducerContractError("invalid usage event: " + "; ".join(errors))
            hash_errors = source_payload_hash_errors(event)
            if hash_errors:
                raise ProducerContractError("invalid usage event: " + "; ".join(hash_errors))
            if event["tenant_reference"] != auth.tenant_reference:
                raise ProducerContractError("usage event tenant does not match delivery context")

            event_id = str(event["event_id"])
            source_event_key = str(event["source_event_key"])
            event_json = json.dumps(
                dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            now_text = _format_instant(self._clock())
            self._begin_write()
            try:
                existing = self._connection.execute(
                    """
                    SELECT outbox_event_id, source_event_key, tenant_reference, event_json
                    FROM producer_outbox_event
                    WHERE outbox_event_id = ?
                       OR (tenant_reference = ? AND source_event_key = ?)
                    ORDER BY outbox_event_id
                    LIMIT 1
                    """,
                    (event_id, auth.tenant_reference, source_event_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["tenant_reference"] == auth.tenant_reference
                        and existing["source_event_key"] == source_event_key
                        and existing["event_json"] == event_json
                    ):
                        self._connection.commit()
                        return ProducerEnqueueReceipt(event_id, source_event_key, True)
                    raise ProducerOutboxConflict(
                        "source_event_key already identifies a different producer fact"
                    )
                self._connection.execute(
                    """
                    INSERT INTO producer_outbox_event
                        (outbox_event_id, tenant_reference, source_event_key, event_json,
                         credential_reference, purpose_code, correlation_id, delivery_status,
                         attempt_count, next_attempt_at, lease_until, last_error_code, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?)
                    """,
                    (
                        event_id,
                        auth.tenant_reference,
                        source_event_key,
                        event_json,
                        auth.credential_reference,
                        auth.purpose_code,
                        auth.correlation_id,
                        now_text,
                        now_text,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return ProducerEnqueueReceipt(event_id, source_event_key, False)

    def get_status(self, outbox_event_id: str) -> ProducerOutboxStatus | None:
        """Return one event status without exposing its payload or credentials."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT outbox_event_id, source_event_key, delivery_status, attempt_count,
                       next_attempt_at, last_error_code
                FROM producer_outbox_event
                WHERE outbox_event_id = ?
                """,
                (outbox_event_id,),
            ).fetchone()
            if row is None:
                return None
            return ProducerOutboxStatus(
                outbox_event_id=row["outbox_event_id"],
                source_event_key=row["source_event_key"],
                status=row["delivery_status"],
                attempt_count=row["attempt_count"],
                next_attempt_at=_parse_instant(row["next_attempt_at"]),
                last_error_code=row["last_error_code"],
            )

    def drain(
        self,
        transport: ProducerOutboxTransport,
        *,
        auth: ProducerAuthContext,
        credential: str | None = None,
        now: datetime | None = None,
        limit: int = 50,
    ) -> ProducerDrainReceipt:
        """Deliver one bounded leased batch and apply partial results atomically."""
        with self._lock:
            _validate_batch_limit(limit)
            current = self._clock() if now is None else now
            now_text = _format_instant(current)
            rows = self._claim_pending(now_text, limit, auth.tenant_reference)
            if not rows:
                return ProducerDrainReceipt(0, 0, 0, 0, 0, 0, ())
            events = tuple(json.loads(row["event_json"]) for row in rows)
            try:
                raw_results = transport.ingest_batch(events, auth=auth, credential=credential)
                result_by_key = _index_results(raw_results, tuple(row["source_event_key"] for row in rows))
            except Exception as error:
                error_code = type(error).__name__
                result_by_key = {
                    row["source_event_key"]: ProducerDeliveryResult(
                        row["source_event_key"], "retryable", error_code
                    )
                    for row in rows
                }

            counts = [0, 0, 0, 0, 0]
            ordered_results = tuple(
                result_by_key[row["source_event_key"]] for row in rows
            )
            self._begin_write()
            try:
                for row, result in zip(rows, ordered_results):
                    dead_lettered = self._apply_result(row, result, current)
                    if result.outcome == "accepted":
                        counts[0] += 1
                    elif result.outcome == "duplicate_replay":
                        counts[1] += 1
                    elif result.outcome == "rejected":
                        counts[2] += 1
                    if dead_lettered:
                        counts[4] += 1
                    elif result.outcome == "retryable":
                        counts[3] += 1
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            return ProducerDrainReceipt(len(rows), *counts, ordered_results)

    def _initialize_schema(self) -> None:
        """Create the small durable queue and its due-event index."""
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS producer_outbox_event (
                outbox_event_id TEXT PRIMARY KEY,
                tenant_reference TEXT NOT NULL,
                source_event_key TEXT NOT NULL,
                event_json TEXT NOT NULL,
                credential_reference TEXT,
                purpose_code TEXT NOT NULL,
                correlation_id TEXT,
                delivery_status TEXT NOT NULL CHECK (delivery_status IN ('pending', 'delivered', 'dead_letter')),
                attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
                next_attempt_at TEXT NOT NULL,
                lease_until TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (tenant_reference, source_event_key)
            );
            CREATE INDEX IF NOT EXISTS producer_outbox_due_event
                ON producer_outbox_event (delivery_status, next_attempt_at, lease_until);
            """
        )

    def _begin_write(self) -> None:
        """Serialize claims and state transitions across local workers."""
        self._connection.execute("BEGIN IMMEDIATE")

    def _claim_pending(
        self, now_text: str, limit: int, tenant_reference: str
    ) -> tuple[sqlite3.Row, ...]:
        """Lease due rows so a crashed worker can be recovered after expiry."""
        lease_until = _format_instant(
            _parse_instant(now_text) + timedelta(seconds=self._lease_seconds)
        )
        self._begin_write()
        try:
            rows = tuple(
                self._connection.execute(
                    """
                    SELECT outbox_event_id, source_event_key, event_json, attempt_count
                    FROM producer_outbox_event
                    WHERE tenant_reference = ?
                      AND delivery_status = 'pending'
                      AND next_attempt_at <= ?
                      AND (lease_until IS NULL OR lease_until <= ?)
                    ORDER BY next_attempt_at, outbox_event_id
                    LIMIT ?
                    """,
                    (tenant_reference, now_text, now_text, limit),
                ).fetchall()
            )
            for row in rows:
                self._connection.execute(
                    """
                    UPDATE producer_outbox_event
                    SET attempt_count = attempt_count + 1, lease_until = ?
                    WHERE outbox_event_id = ?
                    """,
                    (lease_until, row["outbox_event_id"]),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return tuple(
            {
                "outbox_event_id": row["outbox_event_id"],
                "source_event_key": row["source_event_key"],
                "event_json": row["event_json"],
                "attempt_count": row["attempt_count"] + 1,
            }
            for row in rows
        )

    def _apply_result(
        self, row: Mapping[str, Any], result: ProducerDeliveryResult, now: datetime
    ) -> bool:
        """Persist one terminal, retryable, or dead-letter outcome."""
        event_id = row["outbox_event_id"]
        if result.outcome in {"accepted", "duplicate_replay"}:
            self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'delivered', lease_until = NULL, last_error_code = NULL
                WHERE outbox_event_id = ?
                """,
                (event_id,),
            )
            return False
        if result.outcome == "rejected":
            self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'dead_letter', lease_until = NULL,
                    last_error_code = ?
                WHERE outbox_event_id = ?
                """,
                (result.reason_code or "rejected", event_id),
            )
            return True
        if row["attempt_count"] >= self._max_attempts:
            self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'dead_letter', lease_until = NULL,
                    last_error_code = ?
                WHERE outbox_event_id = ?
                """,
                (result.reason_code or "retry_exhausted", event_id),
            )
            return True
        backoff = min(
            self._max_backoff_seconds,
            self._base_backoff_seconds * (2 ** (row["attempt_count"] - 1)),
        )
        self._connection.execute(
            """
            UPDATE producer_outbox_event
            SET delivery_status = 'pending', lease_until = NULL,
                next_attempt_at = ?, last_error_code = ?
            WHERE outbox_event_id = ?
            """,
            (
                _format_instant(now + timedelta(seconds=backoff)),
                result.reason_code or "retryable",
                event_id,
            ),
        )
        return False


def _validate_batch_limit(limit: int) -> None:
    """Bound batch size before it reaches the transport or SQL LIMIT."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 through 100")


def _index_results(
    raw_results: Sequence[ProducerDeliveryResult], expected_keys: tuple[str, ...]
) -> dict[str, ProducerDeliveryResult]:
    """Validate a partial receipt and synthesize retryable missing results."""
    expected = set(expected_keys)
    indexed: dict[str, ProducerDeliveryResult] = {}
    try:
        results = tuple(raw_results)
    except TypeError as error:
        raise ProducerOutboxError("transport returned an invalid result sequence") from error
    for result in results:
        if not isinstance(result, ProducerDeliveryResult):
            raise ProducerOutboxError("transport returned an invalid delivery result")
        if result.source_event_key not in expected:
            raise ProducerOutboxError("transport returned an unknown source_event_key")
        if result.source_event_key in indexed:
            raise ProducerOutboxError("transport returned duplicate source_event_key results")
        indexed[result.source_event_key] = result
    for source_event_key in expected_keys:
        if source_event_key not in indexed:
            indexed[source_event_key] = ProducerDeliveryResult(
                source_event_key, "retryable", "missing_transport_receipt"
            )
    return indexed


def _format_instant(value: datetime) -> str:
    """Render an aware instant in stable UTC form."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_instant(value: str) -> datetime:
    """Parse the outbox's own UTC timestamp representation."""
    if not isinstance(value, str):
        raise ValueError("outbox timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("outbox timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = (
    "ProducerAuthContext",
    "ProducerDeliveryResult",
    "ProducerDrainReceipt",
    "ProducerEnqueueReceipt",
    "ProducerOutbox",
    "ProducerOutboxConflict",
    "ProducerOutboxError",
    "ProducerOutboxStatus",
    "ProducerOutboxTransport",
)
