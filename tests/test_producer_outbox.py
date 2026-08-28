"""Tests for durable producer buffering and partial batch delivery."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from unittest import mock

from metering_billing.producer_outbox import (
    ProducerAuthContext,
    ProducerDeliveryResult,
    ProducerOutbox,
    ProducerOutboxConflict,
    ProducerOutboxError,
    _format_instant,
    _index_results,
    _parse_instant,
    _validate_batch_limit,
)
from metering_billing.payload_integrity import compute_source_payload_hash
from metering_billing.producer_sdk import ProducerContractError, build_usage_event


FIXTURE_PATH = (
    Path(__file__).parents[1] / "schemas" / "examples" / "usage-event-v1-conformance.json"
)
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
AUTH = ProducerAuthContext(
    tenant_reference="urn:cwl:tenant_001",
    purpose_code="usage_delivery",
    credential_reference="urn:cwl:credential_001",
    correlation_id="corr-001",
)


class FakeTransport:
    """Capture an outbox call and return configured per-event results."""

    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = results
        self.error = error
        self.events = ()
        self.auth = None
        self.credential = None

    def ingest_batch(self, events, *, auth, credential):
        self.events = tuple(events)
        self.auth = auth
        self.credential = credential
        if self.error is not None:
            raise self.error
        if callable(self.results):
            return self.results(self.events)
        return self.results or ()


class BlockingTransport(FakeTransport):
    """Hold delivery I/O so local enqueue can be checked independently."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def ingest_batch(self, events, *, auth, credential):
        self.events = tuple(events)
        self.auth = auth
        self.credential = credential
        self.started.set()
        self.release.wait(timeout=2)
        return [ProducerDeliveryResult(event["source_event_key"], "accepted") for event in events]


def event_for(index: int, tenant_reference: str | None = None) -> dict[str, object]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    arguments = dict(fixture["event"])
    arguments.pop("source_payload_hash")
    arguments["event_id"] = UUID(int=index + 1)
    arguments["source_event_key"] = f"producer-event-{index}"
    if tenant_reference is not None:
        arguments["tenant_reference"] = tenant_reference
    return build_usage_event(**arguments)


class ProducerOutboxTests(unittest.TestCase):
    """Exercise persistence, replay, retry, and dead-letter boundaries."""

    def test_context_and_delivery_result_reject_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "tenant_reference"):
            ProducerAuthContext("", "usage_delivery")
        with self.assertRaisesRegex(ValueError, "purpose_code"):
            ProducerAuthContext("urn:cwl:tenant_001", " ")
        with self.assertRaisesRegex(ValueError, "credential_reference"):
            ProducerAuthContext("urn:cwl:tenant_001", "usage_delivery", credential_reference="")
        with self.assertRaisesRegex(ValueError, "correlation_id"):
            ProducerAuthContext("urn:cwl:tenant_001", "usage_delivery", correlation_id=1)
        with self.assertRaisesRegex(ValueError, "source_event_key"):
            ProducerDeliveryResult("", "accepted")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ProducerDeliveryResult("event-1", "unknown")

    def test_configuration_and_private_timestamp_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            for name, value in (
                ("max_attempts", 0),
                ("base_backoff_seconds", 0),
                ("max_backoff_seconds", 0),
                ("lease_seconds", 0),
            ):
                kwargs = {name: value}
                with self.assertRaises(ValueError):
                    ProducerOutbox(path, **kwargs)
            outbox = ProducerOutbox(path)
            outbox.close()

        for invalid in (None, "2026-08-28T12:00:00"):
            with self.assertRaises(ValueError):
                _format_instant(invalid)
        with self.assertRaises(ValueError):
            _parse_instant(1)
        with self.assertRaises(ValueError):
            _parse_instant("2026-08-28T12:00:00")
        for invalid in (0, True, 101):
            with self.assertRaises(ValueError):
                _validate_batch_limit(invalid)

    def test_enqueue_is_validated_idempotent_and_durable_without_secret(self) -> None:
        event = event_for(1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            outbox = ProducerOutbox(path, clock=lambda: NOW)
            columns = {
                row[1]
                for row in outbox._connection.execute(
                    "PRAGMA table_info(producer_outbox_event)"
                )
            }
            self.assertIn("delivery_status", columns)
            self.assertNotIn("status", columns)
            first = outbox.enqueue(event, auth=AUTH)
            replay = outbox.enqueue(event, auth=AUTH)
            self.assertFalse(first.duplicate_enqueue)
            self.assertTrue(replay.duplicate_enqueue)
            self.assertEqual(first.outbox_event_id, str(event["event_id"]))
            self.assertEqual(outbox.get_status(first.outbox_event_id).attempt_count, 0)
            self.assertIsNone(outbox.get_status("missing"))
            outbox.close()

            reopened = ProducerOutbox(path, clock=lambda: NOW)
            self.assertEqual(reopened.get_status(first.outbox_event_id).status, "pending")
            reopened.close()
            self.assertNotIn(b"bearer-secret", path.read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            outbox = ProducerOutbox(Path(directory) / "outbox.sqlite3", clock=lambda: NOW)
            outbox.enqueue(event, auth=AUTH)
            with self.assertRaises(ProducerContractError):
                outbox.enqueue({}, auth=AUTH)
            with self.assertRaises(ProducerContractError):
                outbox.enqueue([], auth=AUTH)
            invalid = dict(event, source_payload_hash="sha256:" + "0" * 64)
            with self.assertRaises(ProducerContractError):
                outbox.enqueue(invalid, auth=AUTH)
            with self.assertRaises(ProducerContractError):
                outbox.enqueue(event, auth=ProducerAuthContext("urn:cwl:other", "usage_delivery"))
            changed = event_for(2)
            changed["source_event_key"] = event["source_event_key"]
            changed["source_payload_hash"] = compute_source_payload_hash(changed)
            with self.assertRaises(ProducerOutboxConflict):
                outbox.enqueue(changed, auth=AUTH)
            same_id_different_key = event_for(3)
            same_id_different_key["event_id"] = event["event_id"]
            with self.assertRaises(ProducerOutboxConflict):
                outbox.enqueue(same_id_different_key, auth=AUTH)
            outbox.close()

    def test_transaction_failures_roll_back_and_claim_failures_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            outbox = ProducerOutbox(path, clock=lambda: NOW)
            event = event_for(50)
            outbox.enqueue(event, auth=AUTH)
            transport = FakeTransport([ProducerDeliveryResult(event["source_event_key"], "accepted")])
            with mock.patch.object(outbox, "_apply_result", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    outbox.drain(transport, auth=AUTH, now=NOW)

            denied = ProducerOutbox(Path(directory) / "denied.sqlite3", clock=lambda: NOW)
            denied.enqueue(event_for(51), auth=AUTH)

            def deny_reads(action, _arg1, _arg2, _database, _source):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK

            denied._connection.set_authorizer(deny_reads)
            with self.assertRaises(sqlite3.DatabaseError):
                denied.drain(FakeTransport([]), auth=AUTH, now=NOW)
            denied.close()
            outbox.close()

    def test_drain_accepts_duplicate_rejects_and_passes_auth_ephemerally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = ProducerOutbox(Path(directory) / "outbox.sqlite3", clock=lambda: NOW)
            accepted = event_for(10)
            duplicate = event_for(11)
            rejected = event_for(12)
            for event in (accepted, duplicate, rejected):
                outbox.enqueue(event, auth=AUTH)
            transport = FakeTransport(
                [
                    ProducerDeliveryResult(accepted["source_event_key"], "accepted"),
                    ProducerDeliveryResult(duplicate["source_event_key"], "duplicate_replay"),
                    ProducerDeliveryResult(rejected["source_event_key"], "rejected", "schema_invalid"),
                ]
            )
            receipt = outbox.drain(
                transport, auth=AUTH, credential="bearer-secret", now=NOW, limit=3
            )
            self.assertEqual(receipt.attempted_event_count, 3)
            self.assertEqual(receipt.accepted_event_count, 1)
            self.assertEqual(receipt.duplicate_replay_count, 1)
            self.assertEqual(receipt.rejected_event_count, 1)
            self.assertEqual(receipt.dead_letter_event_count, 1)
            self.assertEqual(transport.auth, AUTH)
            self.assertEqual(transport.credential, "bearer-secret")
            self.assertEqual(outbox.get_status(str(accepted["event_id"])).status, "delivered")
            self.assertEqual(outbox.get_status(str(duplicate["event_id"])).status, "delivered")
            self.assertEqual(outbox.get_status(str(rejected["event_id"])).status, "dead_letter")

            empty = outbox.drain(transport, auth=AUTH, now=NOW)
            self.assertEqual(empty.attempted_event_count, 0)
            outbox.close()

    def test_drain_is_scoped_to_delivery_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = ProducerOutbox(Path(directory) / "outbox.sqlite3", clock=lambda: NOW)
            tenant_event = event_for(13)
            other_event = event_for(14, "urn:cwl:tenant_002")
            outbox.enqueue(tenant_event, auth=AUTH)
            outbox.enqueue(
                other_event,
                auth=ProducerAuthContext("urn:cwl:tenant_002", "usage_delivery"),
            )
            key = tenant_event["source_event_key"]
            transport = FakeTransport([ProducerDeliveryResult(key, "accepted")])

            receipt = outbox.drain(transport, auth=AUTH, now=NOW)

            self.assertEqual(receipt.attempted_event_count, 1)
            self.assertEqual(transport.events, (tenant_event,))
            self.assertEqual(
                outbox.get_status(str(other_event["event_id"])).status, "pending"
            )
            outbox.close()

    def test_drain_does_not_hold_connection_lock_during_transport_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = ProducerOutbox(Path(directory) / "outbox.sqlite3", clock=lambda: NOW)
            draining = event_for(15)
            enqueued_during_drain = event_for(16)
            outbox.enqueue(draining, auth=AUTH)
            transport = BlockingTransport()
            drain_results = []
            drain_errors = []

            def run_drain() -> None:
                try:
                    drain_results.append(outbox.drain(transport, auth=AUTH, now=NOW))
                except Exception as error:  # pragma: no cover - assertion below reports it
                    drain_errors.append(error)

            drain_thread = threading.Thread(target=run_drain)
            drain_thread.start()
            self.assertTrue(transport.started.wait(timeout=1))
            enqueue_done = threading.Event()

            def run_enqueue() -> None:
                try:
                    outbox.enqueue(enqueued_during_drain, auth=AUTH)
                finally:
                    enqueue_done.set()

            enqueue_thread = threading.Thread(target=run_enqueue)
            enqueue_thread.start()
            try:
                self.assertTrue(enqueue_done.wait(timeout=1))
            finally:
                transport.release.set()
                drain_thread.join(timeout=2)
                enqueue_thread.join(timeout=2)
            self.assertEqual(drain_errors, [])
            self.assertEqual(drain_results[0].accepted_event_count, 1)
            self.assertEqual(
                outbox.get_status(str(enqueued_during_drain["event_id"])).status, "pending"
            )
            outbox.close()

    def test_drain_retries_with_backoff_then_delivers_after_persistence_reopen(self) -> None:
        event = event_for(20)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            outbox = ProducerOutbox(
                path, clock=lambda: NOW, base_backoff_seconds=5, max_backoff_seconds=6
            )
            outbox.enqueue(event, auth=AUTH)
            key = event["source_event_key"]
            retry = FakeTransport([ProducerDeliveryResult(key, "retryable", "timeout")])
            first = outbox.drain(retry, auth=AUTH, now=NOW)
            self.assertEqual(first.retry_scheduled_event_count, 1)
            status = outbox.get_status(str(event["event_id"]))
            self.assertEqual(status.attempt_count, 1)
            self.assertEqual(status.next_attempt_at, NOW + timedelta(seconds=5))
            not_due = outbox.drain(retry, auth=AUTH, now=NOW + timedelta(seconds=1))
            self.assertEqual(not_due.attempted_event_count, 0)
            outbox.close()

            reopened = ProducerOutbox(path, clock=lambda: NOW, max_attempts=2)
            delivered = FakeTransport([ProducerDeliveryResult(key, "accepted")])
            second = reopened.drain(
                delivered,
                auth=AUTH,
                now=NOW + timedelta(seconds=5, microseconds=500_000),
            )
            self.assertEqual(second.accepted_event_count, 1)
            self.assertEqual(reopened.get_status(str(event["event_id"])).status, "delivered")
            reopened.close()

    def test_drain_dead_letters_exhausted_retry_and_transport_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = ProducerOutbox(
                Path(directory) / "outbox.sqlite3",
                clock=lambda: NOW,
                max_attempts=1,
                base_backoff_seconds=1,
            )
            event = event_for(30)
            outbox.enqueue(event, auth=AUTH)
            transport = FakeTransport(error=TimeoutError("temporary unavailable"))
            receipt = outbox.drain(transport, auth=AUTH, now=NOW)
            self.assertEqual(receipt.dead_letter_event_count, 1)
            self.assertEqual(receipt.event_results[0].reason_code, "TimeoutError")
            self.assertEqual(outbox.get_status(str(event["event_id"])).last_error_code, "TimeoutError")

            missing = event_for(31)
            retrying_outbox = ProducerOutbox(
                Path(directory) / "retrying-outbox.sqlite3", clock=lambda: NOW
            )
            retrying_outbox.enqueue(missing, auth=AUTH)
            missing_receipt = retrying_outbox.drain(FakeTransport([]), auth=AUTH, now=NOW)
            self.assertEqual(missing_receipt.retry_scheduled_event_count, 1)
            self.assertEqual(
                missing_receipt.event_results[0].reason_code, "missing_transport_receipt"
            )
            retrying_outbox.close()
            outbox.close()

    def test_invalid_transport_receipts_fail_closed_as_retryable(self) -> None:
        invalid_results = (
            [ProducerDeliveryResult("unknown", "accepted")],
            [ProducerDeliveryResult("producer-event-41", "accepted")] * 2,
            [object()],
        )
        for index, results in enumerate(invalid_results):
            with tempfile.TemporaryDirectory() as directory:
                outbox = ProducerOutbox(
                    Path(directory) / "outbox.sqlite3", clock=lambda: NOW
                )
                event = event_for(40 + index)
                outbox.enqueue(event, auth=AUTH)
                receipt = outbox.drain(FakeTransport(results), auth=AUTH, now=NOW)
                self.assertEqual(receipt.retry_scheduled_event_count, 1)
                self.assertEqual(receipt.event_results[0].reason_code, "ProducerOutboxError")
                outbox.close()

    def test_result_indexing_rejects_non_iterable_and_fills_missing(self) -> None:
        with self.assertRaises(ProducerOutboxError):
            _index_results(None, ("one",))
        indexed = _index_results((), ("one", "two"))
        self.assertEqual(indexed["one"].outcome, "retryable")
        self.assertEqual(indexed["two"].reason_code, "missing_transport_receipt")


if __name__ == "__main__":
    unittest.main()
