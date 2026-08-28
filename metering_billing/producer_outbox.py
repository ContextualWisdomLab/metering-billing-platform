"""Durable producer delivery for canonical usage events.

The module exposes both the context-scoped leased outbox and the original
minimal sender-injected outbox used by the canonical SDK integration.  Both
store only validated usage events and never own bearer credentials or retry
schedulers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from metering_billing.contracts import validate_usage_event
from metering_billing.payload_integrity import source_payload_hash_errors
from metering_billing.producer_sdk import ProducerContractError

_DELIVERY_OUTCOMES = frozenset(
    {"accepted", "duplicate_replay", "rejected", "retryable"}
)


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
        # ponytail: lock shared SQLite operations only; use a connection pool for higher throughput.
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
                existing_rows = self._connection.execute(
                    """
                    SELECT outbox_event_id, source_event_key, tenant_reference, event_json,
                           credential_reference, purpose_code, correlation_id
                    FROM producer_outbox_event
                    WHERE outbox_event_id = ?
                       OR (tenant_reference = ? AND source_event_key = ?)
                    """,
                    (event_id, auth.tenant_reference, source_event_key),
                ).fetchall()
                if existing_rows:
                    if len(existing_rows) != 1:
                        raise ProducerOutboxConflict(
                            "producer event identity matches multiple persisted facts"
                        )
                    existing = existing_rows[0]
                    if (
                        existing["tenant_reference"] == auth.tenant_reference
                        and existing["source_event_key"] == source_event_key
                        and existing["event_json"] == event_json
                    ):
                        if (
                            existing["credential_reference"] != auth.credential_reference
                            or existing["purpose_code"] != auth.purpose_code
                            or existing["correlation_id"] != auth.correlation_id
                        ):
                            raise ProducerOutboxConflict(
                                "producer fact already exists with a different delivery context"
                            )
                        self._connection.commit()
                        return ProducerEnqueueReceipt(event_id, source_event_key, True)
                    if existing["outbox_event_id"] == event_id:
                        raise ProducerOutboxConflict(
                            "outbox_event_id already identifies a different producer fact"
                        )
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

    def replay_dead_letter(
        self,
        outbox_event_id: str,
        *,
        auth: ProducerAuthContext,
        now: datetime | None = None,
    ) -> None:
        """Return one matching dead-letter event to pending for explicit replay."""
        next_attempt_at = _format_instant(self._clock() if now is None else now)
        with self._lock:
            self._begin_write()
            try:
                updated = self._connection.execute(
                    """
                    UPDATE producer_outbox_event
                    SET delivery_status = 'pending', attempt_count = 0,
                        next_attempt_at = ?, lease_until = NULL, last_error_code = NULL
                    WHERE outbox_event_id = ?
                      AND tenant_reference = ?
                      AND purpose_code = ?
                      AND credential_reference IS ?
                      AND correlation_id IS ?
                      AND delivery_status = 'dead_letter'
                    """,
                    (
                        next_attempt_at,
                        outbox_event_id,
                        auth.tenant_reference,
                        auth.purpose_code,
                        auth.credential_reference,
                        auth.correlation_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise KeyError(outbox_event_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

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
        _validate_batch_limit(limit)
        current = self._clock() if now is None else now
        now_text = _format_instant(current)
        with self._lock:
            rows = self._claim_pending(now_text, limit, auth)
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
        with self._lock:
            self._begin_write()
            try:
                for row, result in zip(rows, ordered_results):
                    applied, dead_lettered = self._apply_result(row, result, current)
                    if not applied:
                        continue
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
        self, now_text: str, limit: int, auth: ProducerAuthContext
    ) -> tuple[dict[str, Any], ...]:
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
                      AND purpose_code = ?
                      AND credential_reference IS ?
                      AND correlation_id IS ?
                      AND delivery_status = 'pending'
                      AND next_attempt_at <= ?
                      AND (lease_until IS NULL OR lease_until <= ?)
                    ORDER BY next_attempt_at, outbox_event_id
                    LIMIT ?
                    """,
                    (
                        auth.tenant_reference,
                        auth.purpose_code,
                        auth.credential_reference,
                        auth.correlation_id,
                        now_text,
                        now_text,
                        limit,
                    ),
                ).fetchall()
            )
            for row in rows:
                self._connection.execute(
                    """
                    UPDATE producer_outbox_event
                    SET lease_until = ?
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
                "attempt_count": row["attempt_count"],
                "lease_until": lease_until,
            }
            for row in rows
        )

    def _apply_result(
        self, row: Mapping[str, Any], result: ProducerDeliveryResult, now: datetime
    ) -> tuple[bool, bool]:
        """Persist one terminal, retryable, or dead-letter outcome."""
        event_id = row["outbox_event_id"]
        lease_until = row["lease_until"]
        if result.outcome in {"accepted", "duplicate_replay"}:
            updated = self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'delivered', attempt_count = attempt_count + 1,
                    lease_until = NULL, last_error_code = NULL
                WHERE outbox_event_id = ? AND delivery_status = 'pending'
                  AND lease_until = ?
                """,
                (event_id, lease_until),
            ).rowcount
            return bool(updated), False
        attempt_count = row["attempt_count"] + 1
        if result.outcome == "rejected":
            updated = self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'dead_letter', attempt_count = ?, lease_until = NULL,
                    last_error_code = ?
                WHERE outbox_event_id = ? AND delivery_status = 'pending'
                  AND lease_until = ?
                """,
                (attempt_count, result.reason_code or "rejected", event_id, lease_until),
            ).rowcount
            return bool(updated), bool(updated)
        if attempt_count >= self._max_attempts:
            updated = self._connection.execute(
                """
                UPDATE producer_outbox_event
                SET delivery_status = 'dead_letter', attempt_count = ?, lease_until = NULL,
                    last_error_code = ?
                WHERE outbox_event_id = ? AND delivery_status = 'pending'
                  AND lease_until = ?
                """,
                (attempt_count, result.reason_code or "retry_exhausted", event_id, lease_until),
            ).rowcount
            return bool(updated), bool(updated)
        backoff = min(
            self._max_backoff_seconds,
            self._base_backoff_seconds * (2 ** (attempt_count - 1)),
        )
        updated = self._connection.execute(
            """
            UPDATE producer_outbox_event
            SET delivery_status = 'pending', attempt_count = ?, lease_until = NULL,
                next_attempt_at = ?, last_error_code = ?
            WHERE outbox_event_id = ? AND delivery_status = 'pending'
              AND lease_until = ?
            """,
            (
                attempt_count,
                _format_instant(now + timedelta(seconds=backoff)),
                result.reason_code or "retryable",
                event_id,
                lease_until,
            ),
        ).rowcount
        return bool(updated), False


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


DeliverySender = Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]]


class DeliveryError(RuntimeError):
    """Base class for sender failures that leave the event in the outbox."""


class TransientDeliveryError(DeliveryError):
    """The sender may succeed on a later bounded attempt."""


class PermanentDeliveryError(DeliveryError):
    """The batch cannot succeed without operator action."""


def _same_https_origin(source_url: str, target_url: str) -> bool:
    """Return whether two URLs share the HTTPS origin used for delivery."""
    source = urlparse(source_url)
    target = urlparse(target_url)
    try:
        return (
            source.scheme == "https"
            and target.scheme == "https"
            and source.hostname == target.hostname
            and (source.port or 443) == (target.port or 443)
        )
    except ValueError:
        return False


class _HttpsSameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow only same-origin HTTPS redirects for credential-bearing sends."""

    def redirect_request(self, request, file, code, message, headers, new_url):
        if not _same_https_origin(request.full_url, new_url):
            raise PermanentDeliveryError("unsafe_redirect")
        return super().redirect_request(request, file, code, message, headers, new_url)


urlopen = build_opener(_HttpsSameOriginRedirectHandler).open


@dataclass(frozen=True)
class OutboxFlushResult:
    """One bounded flush outcome, including events still pending.

    ``rejected_count`` describes a delivery outcome; ``dead_lettered_count``
    describes a queue state transition, so the counters may overlap.
    """

    attempted_count: int
    accepted_count: int
    duplicate_replay_count: int
    rejected_count: int
    retried_count: int
    dead_lettered_count: int
    pending_count: int


class DurableUsageOutbox:
    """SQLite-backed at-least-once delivery queue for usage events.

    The database is local to one producer process.  A crashed send leaves the
    row pending; the server's source-event idempotency makes a later replay
    safe.  ``max_attempts`` is enforced per row, not per process lifetime.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_outbox (
                event_id TEXT PRIMARY KEY,
                event_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK (state IN ('pending', 'dead_letter')),
                last_error_code TEXT
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        """Close the local durable queue."""
        self._connection.close()

    def enqueue(self, event: Mapping[str, Any]) -> None:
        """Persist one event, rejecting an event-id collision with new bytes."""
        errors = validate_usage_event(event)
        if errors:
            raise ProducerContractError("invalid usage event: " + "; ".join(errors))
        hash_errors = source_payload_hash_errors(event)
        if hash_errors:
            raise ProducerContractError("invalid usage event: " + "; ".join(hash_errors))
        event_id = event["event_id"]
        if not isinstance(event_id, str):
            raise ProducerContractError("event_id must be a string")
        event_json = json.dumps(
            dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        existing = self._connection.execute(
            "SELECT event_json FROM usage_outbox WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != event_json:
                raise ProducerContractError(
                    "event_id already has different event bytes"
                )
            return
        self._connection.execute(
            "INSERT INTO usage_outbox(event_id, event_json, attempts, state) VALUES (?, ?, 0, 'pending')",
            (event_id, event_json),
        )
        self._connection.commit()

    def replay_dead_letter(self, event_id: str) -> None:
        """Move one dead-lettered event back to pending for an explicit replay."""
        cursor = self._connection.execute(
            """
            UPDATE usage_outbox
               SET attempts = 0, state = 'pending', last_error_code = NULL
             WHERE event_id = ? AND state = 'dead_letter'
            """,
            (event_id,),
        )
        self._connection.commit()
        if cursor.rowcount != 1:
            raise KeyError(event_id)

    def pending_count(self) -> int:
        """Return the number of events eligible for delivery."""
        row = self._connection.execute(
            "SELECT COUNT(*) FROM usage_outbox WHERE state = 'pending'"
        ).fetchone()
        return int(row[0])

    def dead_letter_count(self) -> int:
        """Return the number of events requiring explicit operator replay."""
        row = self._connection.execute(
            "SELECT COUNT(*) FROM usage_outbox WHERE state = 'dead_letter'"
        ).fetchone()
        return int(row[0])

    def flush(
        self,
        sender: DeliverySender,
        *,
        batch_size: int = 100,
        max_attempts: int = 5,
    ) -> OutboxFlushResult:
        """Send at most one bounded batch and apply its partial receipt.

        Missing or malformed per-event receipts remain retryable and never
        delete a fact.  The caller schedules another flush; this keeps retry
        timing out of the SDK and avoids hidden sleeps in request paths.
        """
        if batch_size < 1 or max_attempts < 1:
            raise ValueError("batch_size and max_attempts must be positive")
        rows = self._connection.execute(
            """
            SELECT event_id, event_json
              FROM usage_outbox
             WHERE state = 'pending'
             ORDER BY rowid
             LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        events = [json.loads(row[1]) for row in rows]
        if not events:
            return OutboxFlushResult(0, 0, 0, 0, 0, 0, 0)

        accepted = replayed = rejected = retried = dead_lettered = 0
        try:
            response = sender(events)
        except PermanentDeliveryError:
            for event_id, _ in rows:
                dead_lettered += self._fail(
                    event_id, "transport_permanent", max_attempts, force_dead=True
                )
        except Exception:
            for event_id, _ in rows:
                dead_lettered += self._fail(
                    event_id, "transport_transient", max_attempts
                )
                retried += self._was_retried(event_id, max_attempts)
        else:
            receipts = (
                response.get("event_receipts")
                if isinstance(response, Mapping)
                else None
            )
            if not isinstance(receipts, list):
                for event_id, _ in rows:
                    dead_lettered += self._fail(
                        event_id, "invalid_delivery_response", max_attempts
                    )
                    retried += self._was_retried(event_id, max_attempts)
            else:
                for event_id, event_json in rows:
                    event = json.loads(event_json)
                    receipt = _find_receipt(receipts, event)
                    outcome = receipt.get("ingestion_outcome_code") if receipt else None
                    if outcome in {"accepted", "duplicate_replay"} and _receipt_matches(
                        receipt, event
                    ):
                        self._connection.execute(
                            "DELETE FROM usage_outbox WHERE event_id = ?", (event_id,)
                        )
                        if outcome == "accepted":
                            accepted += 1
                        else:
                            replayed += 1
                    elif outcome == "rejected" and _receipt_matches(receipt, event):
                        reason = receipt.get("rejection_reason_code")
                        error_code = reason if isinstance(reason, str) else "rejected"
                        dead_lettered += self._fail(
                            event_id, error_code, max_attempts, force_dead=True
                        )
                        rejected += 1
                    else:
                        dead_lettered += self._fail(
                            event_id, "invalid_delivery_receipt", max_attempts
                        )
                        retried += self._was_retried(event_id, max_attempts)
                self._connection.commit()

        self._connection.commit()
        return OutboxFlushResult(
            attempted_count=len(events),
            accepted_count=accepted,
            duplicate_replay_count=replayed,
            rejected_count=rejected,
            retried_count=retried,
            dead_lettered_count=dead_lettered,
            pending_count=self.pending_count(),
        )

    def _fail(
        self,
        event_id: str,
        error_code: str,
        max_attempts: int,
        *,
        force_dead: bool = False,
    ) -> int:
        row = self._connection.execute(
            "SELECT attempts FROM usage_outbox WHERE event_id = ? AND state = 'pending'",
            (event_id,),
        ).fetchone()
        if row is None:
            return 0
        attempts = int(row[0]) + 1
        state = "dead_letter" if force_dead or attempts >= max_attempts else "pending"
        self._connection.execute(
            "UPDATE usage_outbox SET attempts = ?, state = ?, last_error_code = ? WHERE event_id = ?",
            (attempts, state, error_code, event_id),
        )
        return 1 if state == "dead_letter" else 0

    def _was_retried(self, event_id: str, max_attempts: int) -> int:
        row = self._connection.execute(
            "SELECT attempts, state FROM usage_outbox WHERE event_id = ?", (event_id,)
        ).fetchone()
        return int(
            row is not None and row[1] == "pending" and int(row[0]) < max_attempts
        )


class HttpUsageIngestionTransport:
    """Small stdlib sender for the platform's existing batch endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
            raise ValueError("endpoint must be an absolute HTTPS URL")
        self.endpoint = endpoint
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.timeout = timeout

    def __call__(self, events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        request = Request(
            self.endpoint,
            data=json.dumps({"events": list(events)}, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers=self.headers,
            method="POST",
        )
        try:
            with urlopen(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                request, timeout=self.timeout
            ) as response:
                status = int(response.status)
                try:
                    body = json.loads(response.read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise TransientDeliveryError("invalid_json") from error
        except HTTPError as error:
            if error.code >= 500:
                raise TransientDeliveryError("http_5xx") from error
            if error.code in {408, 429}:
                raise TransientDeliveryError(f"http_{error.code}") from error
            try:
                body = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PermanentDeliveryError(f"http_{error.code}") from error
            status = error.code
        except (URLError, TimeoutError, OSError) as error:
            raise TransientDeliveryError("network") from error
        if status >= 500:
            raise TransientDeliveryError("http_5xx")
        if status in {408, 429}:
            raise TransientDeliveryError(f"http_{status}")
        if status < 200 or (status >= 300 and status != 422):
            raise PermanentDeliveryError(f"http_{status}")
        if not isinstance(body, Mapping) or not isinstance(
            body.get("event_receipts"), list
        ):
            raise TransientDeliveryError("invalid_json")
        return body


def _find_receipt(
    receipts: list[Any], event: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    matches = [
        receipt
        for receipt in receipts
        if isinstance(receipt, Mapping)
        and receipt.get("source_event_key") == event.get("source_event_key")
        and receipt.get("tenant_reference") == event.get("tenant_reference")
    ]
    return matches[0] if len(matches) == 1 else None


def _receipt_matches(
    receipt: Mapping[str, Any] | None, event: Mapping[str, Any]
) -> bool:
    return bool(
        receipt is not None
        and receipt.get("tenant_reference") == event.get("tenant_reference")
        and receipt.get("source_payload_hash") == event.get("source_payload_hash")
        and receipt.get("event_contract_version") == event.get("event_contract_version")
    )


__all__ = (
    "DeliveryError",
    "DeliverySender",
    "DurableUsageOutbox",
    "HttpUsageIngestionTransport",
    "OutboxFlushResult",
    "PermanentDeliveryError",
    "ProducerAuthContext",
    "ProducerDeliveryResult",
    "ProducerDrainReceipt",
    "ProducerEnqueueReceipt",
    "ProducerOutbox",
    "ProducerOutboxConflict",
    "ProducerOutboxError",
    "ProducerOutboxStatus",
    "ProducerOutboxTransport",
    "TransientDeliveryError",
)
