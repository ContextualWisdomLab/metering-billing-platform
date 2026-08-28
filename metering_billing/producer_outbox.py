"""Durable producer delivery for canonical usage events.

The outbox stores only already-validated events.  A sender is deliberately
injected so applications can use their own HTTP client without making the SDK
own credentials or a retry scheduler.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from metering_billing.contracts import validate_usage_event
from metering_billing.payload_integrity import source_payload_hash_errors
from metering_billing.producer_sdk import ProducerContractError

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
    "DurableUsageOutbox",
    "HttpUsageIngestionTransport",
    "OutboxFlushResult",
    "PermanentDeliveryError",
    "TransientDeliveryError",
)
