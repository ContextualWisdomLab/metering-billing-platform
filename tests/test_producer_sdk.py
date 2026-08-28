"""Reference producer SDK and cross-language conformance-vector tests."""

from __future__ import annotations

import json
import unittest
import unittest.mock
import tempfile
from pathlib import Path
from uuid import UUID

from metering_billing.producer_sdk import (
    ProducerContractError,
    build_usage_cloud_event,
    build_usage_event,
)
from metering_billing.producer_outbox import (
    DurableUsageOutbox,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from metering_billing.payload_integrity import canonical_source_payload_json


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "schemas"
    / "examples"
    / "usage-event-v1-conformance.json"
)


class ProducerSdkTests(unittest.TestCase):
    """Keep the producer reference aligned with the published contract."""

    def test_conformance_vector_is_byte_stable_and_cloud_event_compatible(self) -> None:
        """The fixture is the handoff contract for Rust and TypeScript SDKs."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        event = fixture["event"]
        builder_arguments = dict(event)
        builder_arguments.pop("source_payload_hash")

        self.assertEqual(build_usage_event(**builder_arguments), event)
        self.assertEqual(
            canonical_source_payload_json(event),
            fixture["canonical_source_payload_json"],
        )
        self.assertEqual(event["source_payload_hash"], fixture["source_payload_hash"])

        cloud_event = build_usage_cloud_event(
            event, source="urn:cwl:producer:reference-python"
        )
        self.assertEqual(cloud_event["specversion"], "1.0")
        self.assertEqual(cloud_event["id"], event["event_id"])
        self.assertEqual(cloud_event["subject"], event["source_event_key"])
        self.assertEqual(cloud_event["data"], event)

    def test_builder_rejects_float_quantities_and_sensitive_fields(self) -> None:
        """Invalid or sensitive producer input cannot cross the SDK boundary."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        arguments = dict(fixture["event"])
        arguments.pop("source_payload_hash")

        invalid_quantity = dict(arguments)
        invalid_quantity["measurements"] = [
            dict(arguments["measurements"][0], quantity=1.25)
        ]
        with self.assertRaises(ProducerContractError):
            build_usage_event(**invalid_quantity)

        sensitive_measurement = dict(
            arguments["measurements"][0], prompt="do not persist"
        )
        invalid_sensitive = dict(arguments, measurements=[sensitive_measurement])
        with self.assertRaises(ProducerContractError):
            build_usage_event(**invalid_sensitive)

        with self.assertRaises(ProducerContractError):
            build_usage_event(
                **dict(
                    arguments, measurements=(measurement for measurement in ["text"])
                )
            )

        with self.assertRaises(ProducerContractError):
            build_usage_event(**dict(arguments, measurements=None))

        with self.assertRaises(ProducerContractError):
            build_usage_event(
                **dict(
                    arguments,
                    measurements=[dict(arguments["measurements"][0], quantity="1e3")],
                )
            )

        uuid_event = build_usage_event(
            **dict(arguments, event_id=UUID(arguments["event_id"]))
        )
        self.assertEqual(uuid_event["event_id"], arguments["event_id"])

        with self.assertRaises(ProducerContractError):
            build_usage_cloud_event(arguments, source="")
        with self.assertRaises(ProducerContractError):
            build_usage_cloud_event(arguments, source=None)
        with self.assertRaises(ProducerContractError):
            build_usage_cloud_event({}, source="urn:cwl:producer:test")
        invalid_hash = dict(arguments, source_payload_hash="sha256:" + "0" * 64)
        with self.assertRaises(ProducerContractError):
            build_usage_cloud_event(invalid_hash, source="urn:cwl:producer:test")
        with unittest.mock.patch(
            "metering_billing.producer_sdk.source_payload_hash_errors",
            side_effect=ValueError("hash failure"),
        ):
            with self.assertRaises(ProducerContractError):
                build_usage_cloud_event(
                    fixture["event"], source="urn:cwl:producer:test"
                )

    def test_allowlisted_dimensions_are_hashed_and_unknown_fields_fail_closed(
        self,
    ) -> None:
        """Provider/model attribution is bounded without accepting arbitrary content."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        arguments = dict(fixture["event"])
        arguments.pop("source_payload_hash")
        event = build_usage_event(
            **arguments,
            dimensions={
                "model_code": "gpt-4o-mini",
                "provider_code": "openai",
                "workflow_code": "verified_workflow",
            },
        )
        self.assertEqual(
            event["source_payload_hash"],
            "sha256:48e92ee2293e0c0eda5aaad6de7b4c6657134c6a0200249498c447c8e3aadac9",
        )
        with self.assertRaises(ProducerContractError):
            build_usage_event(**arguments, dimensions={"prompt": "must-not-persist"})
        with self.assertRaises(ProducerContractError):
            build_usage_event(**arguments, dimensions=["must-not-persist"])

    def test_durable_outbox_retries_partial_receipts_and_replays(self) -> None:
        """Only hash-matched accepted receipts remove durable rows."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        event = fixture["event"]
        with tempfile.TemporaryDirectory() as directory:
            outbox = DurableUsageOutbox(Path(directory) / "outbox.sqlite3")
            outbox.enqueue(event)
            calls = 0

            def sender(events: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TransientDeliveryError("offline")
                return {
                    "event_receipts": [
                        {
                            "source_event_key": event["source_event_key"],
                            "event_contract_version": event["event_contract_version"],
                            "source_payload_hash": event["source_payload_hash"],
                            "tenant_reference": event["tenant_reference"],
                            "ingestion_outcome_code": "duplicate_replay",
                            "usage_event_id": event["event_id"],
                        }
                    ]
                }

            first = outbox.flush(sender, max_attempts=3)
            self.assertEqual(first.retried_count, 1)
            self.assertEqual(outbox.pending_count(), 1)
            outbox.close()
            outbox = DurableUsageOutbox(Path(directory) / "outbox.sqlite3")
            second = outbox.flush(sender, max_attempts=3)
            self.assertEqual(second.duplicate_replay_count, 1)
            self.assertEqual(outbox.pending_count(), 0)
            outbox.close()

    def test_durable_outbox_applies_a_partial_batch_receipt(self) -> None:
        """An absent receipt retries only its event, not its acknowledged sibling."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        arguments = dict(fixture["event"])
        arguments.pop("source_payload_hash")
        second_arguments = dict(
            arguments,
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf6ad",
            source_event_key="producer-reference:workflow-381:step-05",
        )
        first_event = build_usage_event(**arguments)
        second_event = build_usage_event(**second_arguments)
        with tempfile.TemporaryDirectory() as directory:
            outbox = DurableUsageOutbox(Path(directory) / "outbox.sqlite3")
            outbox.enqueue(first_event)
            outbox.enqueue(second_event)

            def sender(events: object) -> dict[str, object]:
                batch = list(events)
                return {
                    "event_receipts": [
                        {
                            "source_event_key": batch[0]["source_event_key"],
                            "tenant_reference": batch[0]["tenant_reference"],
                            "event_contract_version": batch[0][
                                "event_contract_version"
                            ],
                            "source_payload_hash": batch[0]["source_payload_hash"],
                            "ingestion_outcome_code": "accepted",
                        }
                    ]
                }

            result = outbox.flush(sender, batch_size=2)
            self.assertEqual(result.accepted_count, 1)
            self.assertEqual(result.retried_count, 1)
            self.assertEqual(outbox.pending_count(), 1)
            outbox.close()

    def test_durable_outbox_dead_letters_rejection_and_explicit_replay(self) -> None:
        """A rejected fact is retained for an operator-selected replay."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        event = fixture["event"]
        with tempfile.TemporaryDirectory() as directory:
            outbox = DurableUsageOutbox(Path(directory) / "outbox.sqlite3")
            outbox.enqueue(event)
            response = {
                "event_receipts": [
                    {
                        "source_event_key": event["source_event_key"],
                        "tenant_reference": event["tenant_reference"],
                        "ingestion_outcome_code": "rejected",
                        "rejection_reason_code": "meter_not_found",
                    }
                ]
            }
            result = outbox.flush(lambda _: response)
            self.assertEqual(result.dead_lettered_count, 1)
            self.assertEqual(outbox.dead_letter_count(), 1)
            outbox.replay_dead_letter(event["event_id"])
            self.assertEqual(outbox.pending_count(), 1)
            outbox.close()

    def test_durable_outbox_dead_letters_permanent_transport_errors(self) -> None:
        """A permanent transport failure cannot consume retry attempts silently."""
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        event = fixture["event"]
        with tempfile.TemporaryDirectory() as directory:
            outbox = DurableUsageOutbox(Path(directory) / "outbox.sqlite3")
            outbox.enqueue(event)

            result = outbox.flush(
                lambda _: (_ for _ in ()).throw(PermanentDeliveryError("bad request")),
                max_attempts=5,
            )

            self.assertEqual(result.dead_lettered_count, 1)
            self.assertEqual(result.retried_count, 0)
            self.assertEqual(outbox.dead_letter_count(), 1)
            outbox.close()


if __name__ == "__main__":
    unittest.main()
