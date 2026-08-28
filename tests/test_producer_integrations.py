"""Producer-specific mappings remain sanitized and hash-stable."""

from __future__ import annotations

import unittest

from metering_billing.contracts import validate_usage_event
from metering_billing.producer_integrations import (
    build_contextual_usage_event,
    build_fast_mlsirm_usage_event,
    build_newsdom_usage_event,
)


IDENTITY = {
    "tenant_reference": "urn:cwl:tenant_001",
    "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
    "billing_principal_reference": "urn:cwl:tenant_001:billing_principal:019d7002",
}


class ProducerIntegrationTests(unittest.TestCase):
    """Exercise the three heterogeneous producer shapes."""

    def test_contextual_maps_measured_and_estimated_token_usage(self) -> None:
        """Provider/model/workflow dimensions carry no prompt or answer data."""
        event = build_contextual_usage_event(
            {
                "usage_record_id": "usage_workflow_01",
                "workflow_run_id": "workflow_01",
                "provider_name": "openai",
                "model_name": "gpt-4o-mini",
                "request_channel": "sync",
                "route_mode": "conduct",
                "prompt_tokens": 12,
                "completion_tokens": 34,
                "measurement_status": "measured",
                "created_at": 1_756_012_800,
            },
            **IDENTITY,
        )
        self.assertEqual(validate_usage_event(event), ())
        self.assertEqual(event["dimensions"]["workflow_code"], "orchestration_workflow")
        self.assertEqual(event["measurements"][0]["quality_code"], "provider_reported")
        self.assertNotIn("prompt", event)
        self.assertNotIn("answer", event)

    def test_newsdom_maps_partial_shard_counts_without_document_content(self) -> None:
        """A shard is independently identified and only count measurements leave the parser."""
        event = build_newsdom_usage_event(
            **IDENTITY,
            document_job_reference="urn:cwl:tenant_001:document_job:01",
            document_id="doc-01",
            shard_reference="urn:cwl:tenant_001:document_shard:01",
            occurred_at="2026-08-28T01:02:03Z",
            pdf_bytes=4096,
            page_count=2,
            ocr_page_count=1,
            extracted_block_count=7,
        )
        self.assertEqual(validate_usage_event(event), ())
        self.assertEqual(
            event["dimensions"]["shard_reference"],
            "urn:cwl:tenant_001:document_shard:01",
        )
        self.assertEqual(
            {measurement["quality_code"] for measurement in event["measurements"]},
            {"locally_measured"},
        )
        self.assertNotIn("doc-01", str(event["measurements"]))

    def test_fast_mlsirm_maps_run_provenance_and_stable_replay_identity(self) -> None:
        """Run, seed, config, backend, and artifact provenance remain allowlisted references."""
        arguments = {
            **IDENTITY,
            "run_reference": "urn:cwl:tenant_001:run:01",
            "artifact_reference": "urn:cwl:tenant_001:artifact:01",
            "configuration_reference": "urn:cwl:tenant_001:configuration:01",
            "seed_reference": "urn:cwl:tenant_001:seed:20260101",
            "model_code": "mls2plm",
            "backend_code": "rust",
            "occurred_at": "2026-08-28T01:02:03Z",
            "response_rows": 8,
            "response_items": 4,
            "artifact_bytes": 512,
        }
        first = build_fast_mlsirm_usage_event(**arguments)
        second = build_fast_mlsirm_usage_event(**arguments)
        self.assertEqual(validate_usage_event(first), ())
        self.assertEqual(first, second)
        self.assertEqual(first["measurements"][1]["quantity"], "32")


if __name__ == "__main__":
    unittest.main()
