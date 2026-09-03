"""Scheduled smoke proof for heterogeneous producer facts and replay safety."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from metering_billing.producer_integrations import (
    build_contextual_usage_event,
    build_fast_mlsirm_usage_event,
    build_newsdom_usage_event,
)
from metering_billing.producer_outbox import (
    ProducerAuthContext,
    ProducerDeliveryResult,
    ProducerOutbox,
)
from metering_billing.usage_ingestion import UsageIngestionService
from test_usage_ingestion import (
    ACCOUNT_ONE,
    CATALOG_START,
    CREDENTIAL_ONE,
    PRINCIPAL_ONE,
    TENANT_ONE,
    seed_ledger,
)


NOW = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
AUTH = ProducerAuthContext(
    tenant_reference=TENANT_ONE,
    purpose_code="scheduled_producer_smoke",
    credential_reference=CREDENTIAL_ONE,
    correlation_id="producer-smoke-20260829",
)


class BillingOutage:
    """Transport that models an unavailable Billing endpoint."""

    def ingest_batch(self, events: Sequence[dict[str, Any]], **_: object) -> object:
        """Fail without consuming or acknowledging any event."""
        del events
        raise OSError("billing_unavailable")


class DirectBilling:
    """Transport seam backed by the real ingestion service for local smoke."""

    def __init__(self, service: UsageIngestionService) -> None:
        """Keep the service used by the smoke transport."""
        self.service = service

    def ingest_batch(
        self,
        events: Sequence[dict[str, Any]],
        *,
        auth: ProducerAuthContext,
        credential: str | None,
    ) -> tuple[ProducerDeliveryResult, ...]:
        """Ingest the batch and translate each server receipt for the outbox."""
        if auth.tenant_reference != TENANT_ONE or credential != "smoke-credential":
            raise AssertionError("smoke transport received the wrong delivery context")
        receipt = self.service.ingest_usage_batch(events)
        return tuple(
            ProducerDeliveryResult(
                source_event_key=result.source_event_key or "missing-source-event-key",
                outcome={
                    "accepted": "accepted",
                    "duplicate_replay": "duplicate_replay",
                    "rejected": "rejected",
                }[result.ingestion_outcome_code.value],
                reason_code=(
                    result.rejection_reason_code.value
                    if result.rejection_reason_code is not None
                    else None
                ),
                tenant_reference=result.tenant_reference,
                event_contract_version=result.event_contract_version,
                source_payload_hash=result.source_payload_hash,
            )
            for result in receipt.event_receipts
        )


def _smoke_events() -> tuple[dict[str, Any], ...]:
    """Build one count-only event from each heterogeneous producer adapter."""
    return (
        build_contextual_usage_event(
            {
                "usage_record_id": "smoke-record-20260829",
                "workflow_run_id": "smoke-workflow-20260829",
                "measurement_status": "measured",
                "provider_name": "openai",
                "model_name": "gpt-4o-mini",
                "route_mode": "sync",
                "prompt_tokens": 12,
                "completion_tokens": 34,
                "created_at": int(NOW.timestamp()),
            },
            tenant_reference=TENANT_ONE,
            billing_account_reference=ACCOUNT_ONE,
            billing_principal_reference=PRINCIPAL_ONE,
            credential_reference=CREDENTIAL_ONE,
        ),
        build_newsdom_usage_event(
            tenant_reference=TENANT_ONE,
            billing_account_reference=ACCOUNT_ONE,
            billing_principal_reference=PRINCIPAL_ONE,
            credential_reference=CREDENTIAL_ONE,
            document_job_reference="urn:cwl:tenant_001:document_job:smoke-20260829",
            document_id="document-smoke-20260829",
            occurred_at=NOW.isoformat().replace("+00:00", "Z"),
            pdf_bytes=2048,
            page_count=2,
            ocr_page_count=1,
            extracted_block_count=7,
        ),
        build_fast_mlsirm_usage_event(
            tenant_reference=TENANT_ONE,
            billing_account_reference=ACCOUNT_ONE,
            billing_principal_reference=PRINCIPAL_ONE,
            credential_reference=CREDENTIAL_ONE,
            run_reference="urn:cwl:tenant_001:run:smoke-20260829",
            artifact_reference="urn:cwl:tenant_001:artifact:smoke-20260829",
            configuration_reference="urn:cwl:tenant_001:configuration:smoke-20260829",
            seed_reference="urn:cwl:tenant_001:seed:smoke-20260829",
            model_code="mls2plm",
            backend_code="numpy",
            occurred_at=NOW.isoformat().replace("+00:00", "Z"),
            response_rows=3,
            response_items=4,
            artifact_bytes=4096,
        ),
    )


def _seed_smoke_ledger() -> UsageIngestionService:
    """Register every meter used by the three count-only smoke events."""
    ledger = seed_ledger()
    for meter_code, unit_code, quality_code in (
        ("gen_ai_input_token", "token", "provider_reported"),
        ("gen_ai_output_token", "token", "provider_reported"),
        ("document_byte", "byte", "locally_measured"),
        ("document_page", "page", "locally_measured"),
        ("document_ocr_page", "page", "locally_measured"),
        ("document_extracted_block", "block", "locally_measured"),
        ("analysis_run", "run", "deterministically_derived"),
        ("analysis_response_cell", "cell", "locally_measured"),
        ("analysis_artifact_byte", "byte", "locally_measured"),
    ):
        meter = ledger.register_meter_definition(
            meter_code, 1, unit_code, "sum", CATALOG_START
        )
        ledger.register_meter_quality_rule(meter.meter_definition_id, quality_code, "billable")
    return UsageIngestionService(ledger)


class ProducerSmokeReplayTests(unittest.TestCase):
    """Exercise the scheduled, count-only producer delivery boundary."""

    def test_three_producers_survive_outage_reopen_and_server_replay(self) -> None:
        """An outage preserves all three facts and replay stores each only once."""
        events = _smoke_events()
        service = _seed_smoke_ledger()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "producer-outbox.sqlite3"
            outbox = ProducerOutbox(
                database_path,
                clock=lambda: NOW,
                base_backoff_seconds=1,
                max_attempts=3,
            )
            for event in events:
                outbox.enqueue(event, auth=AUTH)

            outage = outbox.drain(
                BillingOutage(),
                auth=AUTH,
                credential="smoke-credential",
                now=NOW,
            )
            self.assertEqual(outage.attempted_event_count, 3)
            self.assertEqual(outage.retry_scheduled_event_count, 3)
            self.assertEqual(
                sorted(result.source_event_key for result in outage.event_results),
                sorted(event["source_event_key"] for event in events),
            )
            outbox.close()

            reopened = ProducerOutbox(
                database_path,
                clock=lambda: NOW,
                base_backoff_seconds=1,
                max_attempts=3,
            )
            delivered = reopened.drain(
                DirectBilling(service),
                auth=AUTH,
                credential="smoke-credential",
                now=NOW.replace(second=1),
            )
            self.assertEqual(delivered.accepted_event_count, 3)
            self.assertEqual(delivered.dead_letter_event_count, 0)
            for event in events:
                replay = reopened.enqueue(event, auth=AUTH)
                self.assertTrue(replay.duplicate_enqueue)
            reopened.close()

        server_replay = service.ingest_usage_batch(events)
        self.assertEqual(server_replay.duplicate_replay_count, 3)
        tenant = service.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(len(service.ledger.stored_usage_set(tenant.tenant_account_id)), 3)


if __name__ == "__main__":
    unittest.main()
