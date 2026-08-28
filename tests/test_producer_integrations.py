"""Producer-specific mappings remain sanitized and hash-stable."""

from __future__ import annotations

import unittest

from metering_billing.contracts import validate_usage_event
from metering_billing.producer_integrations import (
    build_contextual_cloud_event,
    build_contextual_usage_event,
    build_fast_mlsirm_cloud_event,
    build_fast_mlsirm_usage_event,
    build_newsdom_cloud_event,
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

    def test_contextual_cloud_event_and_invalid_identity_fields_fail_closed(
        self,
    ) -> None:
        """The cloud-event wrapper and producer field guards remain bounded."""
        record = {
            "usage_record_id": "usage_request_01",
            "provider_name": "openai",
            "model_name": "gpt-4o-mini",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "created_at": 1,
        }
        cloud_event = build_contextual_cloud_event(record, **IDENTITY)
        self.assertEqual(
            cloud_event["source"], "urn:cwl:producer:contextual-orchestrator"
        )
        with self.assertRaises(ValueError):
            build_contextual_usage_event(
                {**record, "usage_record_id": "", "workflow_run_id": "run"},
                **IDENTITY,
            )
        with self.assertRaises(ValueError):
            build_contextual_usage_event(
                {**record, "workflow_run_id": object()},
                **IDENTITY,
            )
        with self.assertRaises(ValueError):
            build_contextual_usage_event(
                {**record, "provider_name": "OpenAI"},
                **IDENTITY,
            )
        with self.assertRaises(ValueError):
            build_contextual_usage_event(
                {**record, "created_at": -1},
                **IDENTITY,
            )

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
            cost_center_reference="urn:cwl:tenant_001:cost_center:document",
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
        self.assertEqual(
            event["cost_center_reference"],
            "urn:cwl:tenant_001:cost_center:document",
        )
        self.assertNotIn("doc-01", str(event["measurements"]))

    def test_newsdom_cloud_event_and_optional_shard_are_supported(self) -> None:
        """A whole-document event remains valid when no shard is supplied."""
        arguments = {
            **IDENTITY,
            "document_job_reference": "urn:cwl:tenant_001:document_job:02",
            "document_id": "doc-02",
            "occurred_at": "2026-08-28T01:02:03Z",
            "pdf_bytes": 1,
            "page_count": 1,
            "ocr_page_count": 0,
            "extracted_block_count": 0,
        }
        cloud_event = build_newsdom_cloud_event(**arguments)
        self.assertEqual(cloud_event["source"], "urn:cwl:producer:newsdom-api")
        self.assertNotIn("shard_reference", cloud_event["data"]["dimensions"])
        with self.assertRaises(ValueError):
            build_newsdom_usage_event(**{**arguments, "document_id": ""})
        with self.assertRaises(ValueError):
            build_newsdom_usage_event(**{**arguments, "pdf_bytes": -1})

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
            "credential_reference": "urn:cwl:tenant_001:credential:01",
            "cost_center_reference": "urn:cwl:tenant_001:cost_center:01",
        }
        first = build_fast_mlsirm_usage_event(**arguments)
        second = build_fast_mlsirm_usage_event(**arguments)
        self.assertEqual(validate_usage_event(first), ())
        self.assertEqual(first, second)
        self.assertEqual(first["measurements"][1]["quantity"], "32")
        self.assertEqual(
            first["credential_reference"], "urn:cwl:tenant_001:credential:01"
        )
        self.assertEqual(
            first["cost_center_reference"], "urn:cwl:tenant_001:cost_center:01"
        )

    def test_fast_mlsirm_cloud_event_and_dimension_guards(self) -> None:
        """Optional artifacts and invalid response shapes remain explicit."""
        arguments = {
            **IDENTITY,
            "run_reference": "urn:cwl:tenant_001:run:02",
            "artifact_reference": "urn:cwl:tenant_001:artifact:02",
            "configuration_reference": "urn:cwl:tenant_001:configuration:02",
            "seed_reference": "urn:cwl:tenant_001:seed:20260102",
            "model_code": "mls2plm",
            "backend_code": "rust",
            "occurred_at": "2026-08-28T01:02:03Z",
            "response_rows": 2,
            "response_items": 3,
        }
        cloud_event = build_fast_mlsirm_cloud_event(**arguments)
        self.assertEqual(cloud_event["source"], "urn:cwl:producer:fast-mlsirm")
        self.assertEqual(len(cloud_event["data"]["measurements"]), 2)
        with self.assertRaises(ValueError):
            build_fast_mlsirm_usage_event(**{**arguments, "response_rows": -1})
        with self.assertRaises(ValueError):
            build_fast_mlsirm_usage_event(
                **{**arguments, "response_rows": 10**18, "response_items": 2}
            )


if __name__ == "__main__":
    unittest.main()
