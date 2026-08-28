"""Reference producer SDK and cross-language conformance-vector tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import UUID

from metering_billing.producer_sdk import (
    ProducerContractError,
    build_usage_cloud_event,
    build_usage_event,
    canonical_source_payload_json,
)


FIXTURE_PATH = (
    Path(__file__).parents[1] / "schemas" / "examples" / "usage-event-v1-conformance.json"
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
            canonical_source_payload_json(event), fixture["canonical_source_payload_json"]
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

        sensitive_measurement = dict(arguments["measurements"][0], prompt="do not persist")
        invalid_sensitive = dict(arguments, measurements=[sensitive_measurement])
        with self.assertRaises(ProducerContractError):
            build_usage_event(**invalid_sensitive)

        with self.assertRaises(ProducerContractError):
            build_usage_event(
                **dict(arguments, measurements=(measurement for measurement in ["text"]))
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


if __name__ == "__main__":
    unittest.main()
