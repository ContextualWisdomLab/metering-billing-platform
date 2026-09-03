"""Realistic usage-ingestion tests for idempotency, isolation, and decimals."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import UUID

import metering_billing.contracts as contracts_module
from metering_billing import (
    ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME,
    IngestionOutcomeCode,
    MemoryUsageLedger,
    RejectionReasonCode,
    TimeWindow,
    UsageIngestionService,
    compute_source_payload_hash,
    default_schemas_directory,
    format_exact_decimal,
    load_json_schema,
    parse_exact_decimal,
    parse_iso8601_datetime,
    validate_usage_event,
)
from metering_billing.contracts import (
    validate_journal_proposal,
    validate_schema_instance,
    validate_usage_ingestion_receipt,
)
from metering_billing.errors import ExactDecimalError, TimeWindowError, require_resolved
from metering_billing.exact_decimal import require_decimal_quantity
from metering_billing.payload_integrity import (
    canonical_source_payload,
    source_payload_hash_errors,
)
from metering_billing.usage_ingestion import validate_event_contract
from metering_billing.usage_ledger import generate_record_id


CATALOG_START = datetime(2026, 1, 1, tzinfo=UTC)
TENANT_ONE = "urn:cwl:tenant_001"
TENANT_TWO = "urn:cwl:tenant_002"
ACCOUNT_ONE = "urn:cwl:tenant_001:billing_account:019d7001"
ACCOUNT_TWO = "urn:cwl:tenant_002:billing_account:019d8001"
PRINCIPAL_ONE = "urn:cwl:tenant_001:billing_principal:019d7002"
PRINCIPAL_TWO = "urn:cwl:tenant_002:billing_principal:019d8002"
CREDENTIAL_ONE = "urn:cwl:tenant_001:credential_record:019d7003"
CREDENTIAL_TWO = "urn:cwl:tenant_002:credential_record:019d8003"


def make_event(**overrides: object) -> dict[str, object]:
    """Build a contract-valid usage event and compute its source-payload hash."""
    event: dict[str, object] = {
        "event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
        "event_contract_version": 1,
        "source_event_key": "workflow_381:step_04:attempt_01",
        "tenant_reference": TENANT_ONE,
        "billing_account_reference": ACCOUNT_ONE,
        "billing_principal_reference": PRINCIPAL_ONE,
        "credential_reference": CREDENTIAL_ONE,
        "product_code": "contextual_orchestrator",
        "occurred_at": "2026-08-16T10:27:42.482Z",
        "measurements": [
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "1810",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ],
    }
    event.update(overrides)
    if "source_payload_hash" not in overrides:
        event["source_payload_hash"] = compute_source_payload_hash(event)
    return event


def known_event_batch() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Return the buyer fixture whose stored set must be reproduced exactly."""
    return (
        make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
            source_event_key="workflow_381:step_04:attempt_01",
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1810",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        ),
        make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf61d",
            source_event_key="workflow_381:step_05:attempt_01",
            occurred_at="2026-08-16T10:28:10.000Z",
            operation_code="complete_step",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "42.5",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        ),
        make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf61e",
            source_event_key="workflow_382:step_01:attempt_01",
            occurred_at="2026-08-16T11:00:00.000Z",
            cost_center_reference="urn:cwl:tenant_001:cost_center:platform",
            project_reference="urn:cwl:tenant_001:project:metering",
            recorded_at="2026-08-16T11:00:01.000Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "0.000000000001",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        ),
    )


def seed_ledger() -> MemoryUsageLedger:
    """Register two isolated tenants with attribution and one billable meter."""
    ledger = MemoryUsageLedger()
    ledger.register_tenant(TENANT_ONE)
    ledger.register_tenant(TENANT_TWO)
    ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE)
    ledger.register_billing_account(TENANT_TWO, ACCOUNT_TWO)
    ledger.register_billing_principal(
        TENANT_ONE, PRINCIPAL_ONE, "github_workflow", CATALOG_START
    )
    ledger.register_billing_principal(
        TENANT_TWO, PRINCIPAL_TWO, "github_workflow", CATALOG_START
    )
    ledger.register_credential_record(
        TENANT_ONE, CREDENTIAL_ONE, "api_key", "fingerprint-one"
    )
    ledger.register_credential_record(
        TENANT_TWO, CREDENTIAL_TWO, "api_key", "fingerprint-two"
    )
    ledger.register_credential_assignment(
        TENANT_ONE, CREDENTIAL_ONE, PRINCIPAL_ONE, ACCOUNT_ONE, CATALOG_START
    )
    ledger.register_credential_assignment(
        TENANT_TWO, CREDENTIAL_TWO, PRINCIPAL_TWO, ACCOUNT_TWO, CATALOG_START
    )
    meter = ledger.register_meter_definition(
        "gen_ai_output_token", 1, "token", "sum", CATALOG_START
    )
    ledger.register_meter_quality_rule(
        meter.meter_definition_id, "provider_reported", "billable"
    )
    ledger.register_meter_quality_rule(
        meter.meter_definition_id, "estimated", "analytics_only"
    )
    return ledger


class UsageIngestionTests(unittest.TestCase):
    """Verify buyer-facing ingest, replay, isolation, and decimal behavior."""

    def test_require_resolved_fails_closed_for_hollow_success(self) -> None:
        """A resolver that reports success without a row must raise ValueError."""
        self.assertEqual(require_resolved("stored_row", "tenant"), "stored_row")
        with self.assertRaisesRegex(ValueError, "tenant resolution succeeded without a stored tenant"):
            require_resolved(None, "tenant")

    def test_ingestion_fails_closed_when_resolvers_return_no_row(self) -> None:
        """Production checks must survive optimized Python execution."""
        service = UsageIngestionService(seed_ledger())
        resolver_cases = (
            ("resolve_tenant", "tenant"),
            ("resolve_billing_account", "billing_account"),
            ("resolve_billing_principal", "billing_principal"),
            ("resolve_credential", "credential"),
            ("resolve_meter", "meter"),
        )
        for resolver_name, fact_name in resolver_cases:
            with self.subTest(resolver_name=resolver_name):
                with mock.patch.object(
                    service.ledger, resolver_name, return_value=(None, None)
                ):
                    with self.assertRaisesRegex(ValueError, f"{fact_name} resolution succeeded"):
                        service.ingest_usage_event(make_event())

    def test_usage_queries_fail_closed_when_tenant_row_is_missing(self) -> None:
        """Tenant-scoped reads must not continue after a hollow resolution."""
        service = UsageIngestionService(seed_ledger())
        window = TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z")
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.query_usage_window(TENANT_ONE, window)
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.query_ingestion_receipts(TENANT_ONE)

    def test_allowlisted_dimensions_survive_ingestion(self) -> None:
        """Provider/model dimensions remain durable without storing content."""
        event = make_event(
            dimensions={
                "model_code": "gpt-4o-mini",
                "provider_code": "openai",
                "workflow_code": "verified_workflow",
            }
        )
        ledger = seed_ledger()
        stored = UsageIngestionService(ledger).ingest_usage_event(event)
        assert stored.usage_event_id is not None
        persisted = ledger.get_usage_event(stored.usage_event_id)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(
            persisted.dimensions,
            (
                ("model_code", "gpt-4o-mini"),
                ("provider_code", "openai"),
                ("workflow_code", "verified_workflow"),
            ),
        )

    def test_contract_metadata_and_meter_version_survive_ingestion(self) -> None:
        """Version and trace metadata remain attached to the immutable usage fact."""
        ledger = seed_ledger()
        event = make_event(
            producer_contract_version=2,
            repository_reference="urn:cwl:repository:producer",
            trace_reference="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            correlation_reference="urn:cwl:correlation:workflow_381",
            causation_reference="urn:cwl:causation:step_03",
            available_at="2026-08-16T10:27:43.482Z",
            correction_lineage={
                "prior_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61b",
                "relationship_code": "corrects",
                "reason_code": "late_provider_reconciliation",
            },
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "meter_version": 1,
                    "quantity": "1810",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        receipt = UsageIngestionService(ledger).ingest_usage_event(event)
        self.assertEqual(receipt.ingestion_outcome_code, IngestionOutcomeCode.ACCEPTED)
        stored = ledger.get_usage_event(receipt.usage_event_id)
        assert stored is not None
        self.assertEqual(stored.producer_contract_version, 2)
        self.assertEqual(stored.repository_reference, event["repository_reference"])
        self.assertEqual(stored.trace_reference, event["trace_reference"])
        self.assertEqual(
            stored.available_at,
            datetime(2026, 8, 16, 10, 27, 43, 482000, tzinfo=UTC),
        )
        self.assertEqual(dict(stored.correction_lineage), event["correction_lineage"])
        self.assertEqual(stored.measurements[0].meter_version, 1)

    def test_unpublished_event_contract_version_is_rejected_before_storage(self) -> None:
        """The server rejects a version without a published schema contract."""
        event = make_event(event_contract_version=2)
        receipt = UsageIngestionService(seed_ledger()).ingest_usage_event(event)
        self.assertEqual(receipt.ingestion_outcome_code, IngestionOutcomeCode.REJECTED)
        self.assertEqual(receipt.rejection_reason_code, RejectionReasonCode.SCHEMA_INVALID)

    def test_known_batch_reproduces_stored_usage_and_rejects_replays(self) -> None:
        """The same batch must store one usage set and then only acknowledge replays."""
        service = UsageIngestionService(seed_ledger())
        batch = known_event_batch()
        first = service.ingest_usage_batch(batch)
        self.assertEqual(first.accepted_event_count, 3)
        self.assertEqual(first.duplicate_replay_count, 0)
        self.assertEqual(first.rejected_event_count, 0)
        self.assertEqual(validate_usage_ingestion_receipt(first.as_contract_dict()), ())

        tenant = service.ledger.require_tenant(TENANT_ONE)
        stored_after_first = service.ledger.stored_usage_set(tenant.tenant_account_id)
        self.assertEqual(len(stored_after_first), 3)
        quantities = {
            measurement.measured_quantity
            for event in service.ledger.usage_events.values()
            for measurement in event.measurements
        }
        self.assertEqual(
            quantities,
            {Decimal("1810"), Decimal("42.5"), Decimal("0.000000000001")},
        )

        second = service.ingest_usage_batch(batch)
        self.assertEqual(second.accepted_event_count, 0)
        self.assertEqual(second.duplicate_replay_count, 3)
        self.assertEqual(second.rejected_event_count, 0)
        self.assertEqual(
            service.ledger.stored_usage_set(tenant.tenant_account_id),
            stored_after_first,
        )
        self.assertEqual(len(service.ledger.accounting_export_records), 0)
        receipts = service.query_ingestion_receipts(TENANT_ONE)
        self.assertEqual(len(receipts), 6)
        self.assertEqual(
            [row.ingestion_outcome_code for row in receipts],
            ["accepted", "accepted", "accepted", "duplicate_replay", "duplicate_replay", "duplicate_replay"],
        )
        self.assertEqual(len(service.query_ingestion_receipts()), 6)
        self.assertEqual(service.query_ingestion_receipts("urn:cwl:missing_tenant"), ())

    def test_time_window_query_stays_inside_tenant_and_bounds(self) -> None:
        """A half-open window returns only that tenant's in-range events."""
        service = UsageIngestionService(seed_ledger())
        batch = known_event_batch()
        service.ingest_usage_batch(batch)
        foreign = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf701",
            source_event_key="tenant_two:step_01",
            tenant_reference=TENANT_TWO,
            billing_account_reference=ACCOUNT_TWO,
            billing_principal_reference=PRINCIPAL_TWO,
            credential_reference=CREDENTIAL_TWO,
            occurred_at="2026-08-16T10:27:42.482Z",
        )
        self.assertEqual(
            service.ingest_usage_event(foreign).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )

        window = TimeWindow.from_iso8601(
            "2026-08-16T10:00:00Z", "2026-08-16T10:30:00Z"
        )
        matched = service.query_usage_window(TENANT_ONE, window)
        self.assertEqual(
            [event.source_event_key for event in matched],
            ["workflow_381:step_04:attempt_01", "workflow_381:step_05:attempt_01"],
        )
        self.assertTrue(
            all(event.tenant_account_id == service.ledger.require_tenant(TENANT_ONE).tenant_account_id for event in matched)
        )
        self.assertEqual(service.query_usage_window("urn:cwl:missing_tenant", window), ())
        tenant_one = service.ledger.require_tenant(TENANT_ONE)
        tenant_two = service.ledger.require_tenant(TENANT_TWO)
        self.assertEqual(len(service.ledger.stored_usage_set(tenant_one.tenant_account_id)), 3)
        self.assertEqual(len(service.ledger.stored_usage_set(tenant_two.tenant_account_id)), 1)

    def test_same_key_with_mutated_payload_is_a_conflict(self) -> None:
        """A retry that changes the commercial fact must not overwrite history."""
        service = UsageIngestionService(seed_ledger())
        original = make_event()
        self.assertEqual(
            service.ingest_usage_event(original).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        mutated = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1811",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ]
        )
        receipt = service.ingest_usage_event(mutated)
        self.assertEqual(receipt.ingestion_outcome_code, IngestionOutcomeCode.REJECTED)
        self.assertEqual(receipt.rejection_reason_code, RejectionReasonCode.SOURCE_EVENT_CONFLICT)
        stored = next(iter(service.ledger.usage_events.values()))
        self.assertEqual(stored.measurements[0].measured_quantity, Decimal("1810"))

    def test_reused_producer_event_id_does_not_overwrite_usage(self) -> None:
        """A new source key cannot reuse a producer event_id to replace history."""
        service = UsageIngestionService(seed_ledger())
        original = make_event()
        first = service.ingest_usage_event(original)
        self.assertEqual(first.ingestion_outcome_code, IngestionOutcomeCode.ACCEPTED)
        colliding = make_event(
            source_event_key="workflow_381:step_04:attempt_02",
            occurred_at="2026-08-16T10:29:00Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "9",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        receipt = service.ingest_usage_event(colliding)
        self.assertEqual(receipt.rejection_reason_code, RejectionReasonCode.PRODUCER_EVENT_CONFLICT)
        stored = service.ledger.usage_events[first.usage_event_id]
        self.assertEqual(stored.measurements[0].measured_quantity, Decimal("1810"))
        self.assertEqual(stored.producer_event_id, UUID(original["event_id"]))
        self.assertNotEqual(stored.usage_event_id, stored.producer_event_id)

    def test_equivalent_decimal_and_timestamp_text_share_one_hash(self) -> None:
        """Canonical hashing treats 1 and 1.0, and Z and +00:00, as one fact."""
        first = make_event(
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1.0",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        second = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf888",
            source_event_key="workflow_381:step_04:attempt_alt",
            occurred_at="2026-08-16T10:27:42.482+00:00",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        self.assertEqual(first["source_payload_hash"], second["source_payload_hash"])
        service = UsageIngestionService(seed_ledger())
        self.assertEqual(
            service.ingest_usage_event(first).ingestion_outcome_code,
            IngestionOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            service.ingest_usage_event(second).rejection_reason_code,
            RejectionReasonCode.PAYLOAD_HASH_CONFLICT,
        )

    def test_same_payload_with_new_source_key_is_a_hash_conflict(self) -> None:
        """Hash-version identity rejects a replay that only changes the envelope key."""
        service = UsageIngestionService(seed_ledger())
        original = make_event()
        service.ingest_usage_event(original)
        replay = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf777",
            source_event_key="workflow_381:step_04:attempt_99",
        )
        receipt = service.ingest_usage_event(replay)
        self.assertEqual(receipt.rejection_reason_code, RejectionReasonCode.PAYLOAD_HASH_CONFLICT)

    def test_cross_tenant_attribution_is_rejected(self) -> None:
        """An event cannot bill another tenant's account, principal, or credential."""
        service = UsageIngestionService(seed_ledger())
        crossed_account = make_event(billing_account_reference=ACCOUNT_TWO)
        self.assertEqual(
            service.ingest_usage_event(crossed_account).rejection_reason_code,
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )
        crossed_principal = make_event(billing_principal_reference=PRINCIPAL_TWO)
        self.assertEqual(
            service.ingest_usage_event(crossed_principal).rejection_reason_code,
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )
        crossed_credential = make_event(credential_reference=CREDENTIAL_TWO)
        self.assertEqual(
            service.ingest_usage_event(crossed_credential).rejection_reason_code,
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )

    def test_schema_and_hash_failures_do_not_store_usage(self) -> None:
        """Malformed or tampered events fail closed and leave the ledger empty."""
        service = UsageIngestionService(seed_ledger())
        self.assertEqual(
            service.ingest_usage_event("not-an-object").rejection_reason_code,
            RejectionReasonCode.SCHEMA_INVALID,
        )
        invalid = make_event()
        invalid["prompt"] = "secret customer content"
        invalid["source_payload_hash"] = compute_source_payload_hash(invalid)
        self.assertEqual(
            service.ingest_usage_event(invalid).rejection_reason_code,
            RejectionReasonCode.SCHEMA_INVALID,
        )
        mismatched = make_event(source_payload_hash="sha256:" + "a" * 64)
        self.assertEqual(
            service.ingest_usage_event(mismatched).rejection_reason_code,
            RejectionReasonCode.PAYLOAD_HASH_MISMATCH,
        )
        self.assertEqual(service.ledger.usage_events, {})
        self.assertEqual(validate_event_contract(make_event()), ())
        self.assertEqual(validate_usage_event(make_event()), ())
        audit_rows = service.query_ingestion_receipts()
        self.assertGreaterEqual(len(audit_rows), 3)
        self.assertTrue(all(row.ingestion_outcome_code == "rejected" for row in audit_rows))
        self.assertIsNone(audit_rows[0].tenant_account_id)

    def test_missing_and_inactive_attribution_fail_closed(self) -> None:
        """Unknown, inactive, or expired attribution cannot create usage."""
        service = UsageIngestionService(seed_ledger())
        self.assertEqual(
            service.ingest_usage_event(make_event(tenant_reference="urn:cwl:missing")).rejection_reason_code,
            RejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            service.ingest_usage_event(
                make_event(billing_account_reference="urn:cwl:tenant_001:billing_account:missing")
            ).rejection_reason_code,
            RejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND,
        )
        self.assertEqual(
            service.ingest_usage_event(
                make_event(billing_principal_reference="urn:cwl:tenant_001:billing_principal:missing")
            ).rejection_reason_code,
            RejectionReasonCode.BILLING_PRINCIPAL_NOT_FOUND,
        )
        self.assertEqual(
            service.ingest_usage_event(
                make_event(credential_reference="urn:cwl:tenant_001:credential_record:missing")
            ).rejection_reason_code,
            RejectionReasonCode.CREDENTIAL_NOT_FOUND,
        )

        suspended = seed_ledger()
        account = suspended.billing_accounts[ACCOUNT_ONE]
        suspended.billing_accounts[ACCOUNT_ONE] = replace(account, account_status_code="suspended")
        self.assertEqual(
            UsageIngestionService(suspended).ingest_usage_event(make_event()).rejection_reason_code,
            RejectionReasonCode.BILLING_ACCOUNT_NOT_ACTIVE,
        )

        expired = seed_ledger()
        principal = expired.billing_principals[PRINCIPAL_ONE]
        expired.billing_principals[PRINCIPAL_ONE] = replace(
            principal,
            valid_from=datetime(2026, 8, 17, tzinfo=UTC),
            valid_to=datetime(2026, 8, 18, tzinfo=UTC),
        )
        too_early = UsageIngestionService(expired).ingest_usage_event(make_event())
        self.assertEqual(too_early.rejection_reason_code, RejectionReasonCode.PRINCIPAL_NOT_EFFECTIVE)
        expired.billing_principals[PRINCIPAL_ONE] = replace(
            principal,
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2026, 8, 16, tzinfo=UTC),
        )
        too_late = UsageIngestionService(expired).ingest_usage_event(make_event())
        self.assertEqual(too_late.rejection_reason_code, RejectionReasonCode.PRINCIPAL_NOT_EFFECTIVE)

    def test_credential_assignment_and_optional_credential_paths(self) -> None:
        """A credential must be assigned at event time; omitting it is allowed."""
        unassigned = seed_ledger()
        unassigned.credential_assignments.clear()
        self.assertEqual(
            UsageIngestionService(unassigned).ingest_usage_event(make_event()).rejection_reason_code,
            RejectionReasonCode.CREDENTIAL_NOT_ASSIGNED,
        )

        service = UsageIngestionService(seed_ledger())
        event = make_event()
        del event["credential_reference"]
        event["source_payload_hash"] = compute_source_payload_hash(event)
        receipt = service.ingest_usage_event(event)
        self.assertEqual(receipt.ingestion_outcome_code, IngestionOutcomeCode.ACCEPTED)
        stored = service.ledger.usage_events[receipt.usage_event_id]
        self.assertIsNone(stored.credential_record_id)

    def test_meter_and_measurement_rejections(self) -> None:
        """Unknown meters, unit drift, forbidden quality, and duplicate meters fail."""
        service = UsageIngestionService(seed_ledger())
        unknown_meter = make_event(
            measurements=[
                {
                    "meter_code": "unknown_meter_code",
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ]
        )
        self.assertEqual(
            service.ingest_usage_event(unknown_meter).rejection_reason_code,
            RejectionReasonCode.METER_NOT_FOUND,
        )
        unit_mismatch = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1",
                    "unit_code": "request",
                    "quality_code": "provider_reported",
                }
            ]
        )
        self.assertEqual(
            service.ingest_usage_event(unit_mismatch).rejection_reason_code,
            RejectionReasonCode.METER_UNIT_MISMATCH,
        )
        version_mismatch = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "meter_version": 2,
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ]
        )
        self.assertEqual(
            service.ingest_usage_event(version_mismatch).rejection_reason_code,
            RejectionReasonCode.METER_VERSION_MISMATCH,
        )
        quality = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "reconstructed",
                }
            ]
        )
        self.assertEqual(
            service.ingest_usage_event(quality).rejection_reason_code,
            RejectionReasonCode.METER_QUALITY_NOT_ALLOWED,
        )
        duplicate = make_event(
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                },
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "2",
                    "unit_code": "token",
                    "quality_code": "estimated",
                },
            ]
        )
        self.assertEqual(
            service.ingest_usage_event(duplicate).rejection_reason_code,
            RejectionReasonCode.MEASUREMENT_METER_DUPLICATE,
        )

        with mock.patch(
            "metering_billing.usage_ingestion.parse_exact_decimal",
            side_effect=ExactDecimalError("bad"),
        ):
            self.assertEqual(
                service.ingest_usage_event(make_event()).rejection_reason_code,
                RejectionReasonCode.MEASUREMENT_QUANTITY_INVALID,
            )

    def test_time_window_and_parse_failures(self) -> None:
        """Events outside a declared flush window, or with unusable times, are rejected."""
        service = UsageIngestionService(seed_ledger())
        window = TimeWindow.from_iso8601("2026-08-16T00:00:00Z", "2026-08-16T01:00:00Z")
        self.assertEqual(
            service.ingest_usage_event(make_event(), time_window=window).rejection_reason_code,
            RejectionReasonCode.EVENT_OUTSIDE_TIME_WINDOW,
        )
        with mock.patch(
            "metering_billing.usage_ingestion.parse_iso8601_datetime",
            side_effect=TimeWindowError("bad"),
        ):
            self.assertEqual(
                service.ingest_usage_event(make_event()).rejection_reason_code,
                RejectionReasonCode.SCHEMA_INVALID,
            )
        with mock.patch(
            "metering_billing.usage_ingestion.parse_iso8601_datetime",
            side_effect=[
                datetime(2026, 8, 16, 10, 27, 42, 482000, tzinfo=UTC),
                TimeWindowError("bad available_at"),
            ],
        ):
            self.assertEqual(
                service.ingest_usage_event(
                    make_event(available_at="2026-08-16T10:27:43.482Z")
                ).rejection_reason_code,
                RejectionReasonCode.SCHEMA_INVALID,
            )

    def test_default_service_clock_and_receipt_optional_fields(self) -> None:
        """The zero-argument service constructs a ledger and a timezone-aware clock."""
        fixed = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        service = UsageIngestionService(seed_ledger(), clock=lambda: fixed)
        accepted = service.ingest_usage_event(make_event(recorded_at="2020-01-01T00:00:00Z"))
        self.assertEqual(
            service.ledger.usage_events[accepted.usage_event_id].recorded_at,
            fixed,
        )
        empty = UsageIngestionService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.ingest_usage_event({"source_event_key": "", "event_contract_version": "x"})
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["source_event_key"], "unavailable_source_event_key")
        self.assertNotIn("event_contract_version", payload)
        self.assertNotIn("source_payload_hash", payload)
        self.assertNotIn("tenant_reference", payload)
        self.assertNotIn("usage_event_id", payload)

    def test_journal_proposal_helper_stays_proposal_only(self) -> None:
        """Usage ingestion must keep the accounting contract proposal-only."""
        proposal = {
            "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61d",
            "proposal_contract_version": 1,
            "idempotency_key": "invoice_019d:issued:v1",
            "tenant_reference": TENANT_ONE,
            "legal_entity_reference": "urn:cwl:entity_001",
            "intended_book_role_code": "primary_statutory",
            "transaction_currency": "KRW",
            "transaction_date": "2026-08-31",
            "accounting_date": "2026-08-31",
            "source_payload_hash": "sha256:" + "a" * 64,
            "proposed_at": "2026-08-31T23:59:59Z",
            "proposal_status": "validated",
            "source_event_references": ["urn:cwl:invoice:019d"],
            "lines": [
                {
                    "line_number": 1,
                    "account_role_code": "accounts_receivable",
                    "debit_amount": "10",
                    "credit_amount": "0",
                },
                {
                    "line_number": 2,
                    "account_role_code": "usage_revenue",
                    "debit_amount": "0",
                    "credit_amount": "10",
                },
            ],
        }
        self.assertEqual(validate_journal_proposal(proposal), ())
        posted = dict(proposal, proposal_status="posted")
        self.assertIn(
            "$.proposal_status: value is not in the allowed enumeration",
            validate_journal_proposal(posted),
        )
        self.assertEqual(
            load_json_schema(ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME)["$id"],
            "https://schemas.contextualwisdomlab.org/metering-billing/accounting-journal-proposal/v1",
        )
        self.assertTrue(default_schemas_directory().is_dir())


class LedgerAndContractUnitTests(unittest.TestCase):
    """Cover catalog edge cases, exact decimals, time windows, and hashing."""

    def test_contract_helpers_keep_source_tree_fallbacks(self) -> None:
        """Use repository modules when packaged resource modules are absent."""
        with mock.patch.dict(
            sys.modules,
            {"metering_billing._repository_validation.validate_repository": None},
        ):
            importlib.reload(contracts_module)
            self.assertTrue(callable(contracts_module.validate_schema_instance))
        with mock.patch.dict(sys.modules, {"metering_billing._schemas": None}):
            self.assertEqual(
                contracts_module.default_schemas_directory(),
                Path(__file__).resolve().parents[1] / "schemas",
            )
        importlib.reload(contracts_module)

    def test_catalog_registration_is_idempotent_and_tenant_bound(self) -> None:
        """Re-registering the same URN returns the same row and cannot cross tenants."""
        ledger = seed_ledger()
        tenant = ledger.register_tenant(TENANT_ONE)
        self.assertIs(ledger.register_tenant(TENANT_ONE), tenant)
        self.assertIs(
            ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE),
            ledger.billing_accounts[ACCOUNT_ONE],
        )
        self.assertIs(
            ledger.register_billing_principal(
                TENANT_ONE, PRINCIPAL_ONE, "github_workflow", CATALOG_START
            ),
            ledger.billing_principals[PRINCIPAL_ONE],
        )
        self.assertIs(
            ledger.register_credential_record(
                TENANT_ONE, CREDENTIAL_ONE, "api_key", "fingerprint-one"
            ),
            ledger.credential_records[CREDENTIAL_ONE],
        )
        meter = ledger.meter_definitions[0]
        self.assertIs(
            ledger.register_meter_definition(
                meter.meter_code, meter.meter_version, meter.unit_code, meter.aggregation_code, CATALOG_START
            ),
            meter,
        )
        self.assertIs(
            ledger.register_meter_quality_rule(
                meter.meter_definition_id, "provider_reported", "billable"
            ),
            ledger.meter_quality_rules[(meter.meter_definition_id, "provider_reported")],
        )
        with self.assertRaises(KeyError):
            ledger.require_tenant("urn:cwl:missing")
        with self.assertRaises(ValueError):
            ledger.register_tenant("tenant_001")
        with self.assertRaises(ValueError):
            ledger.register_tenant("urn:cwl:tenant_001:extra")
        with self.assertRaises(ValueError):
            ledger.register_billing_account(TENANT_ONE, ACCOUNT_TWO)

        other = ledger.require_tenant(TENANT_TWO)
        ledger.billing_accounts[ACCOUNT_ONE] = replace(
            ledger.billing_accounts[ACCOUNT_ONE], tenant_account_id=other.tenant_account_id
        )
        with self.assertRaises(ValueError):
            ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE)
        ledger.billing_principals[PRINCIPAL_ONE] = replace(
            ledger.billing_principals[PRINCIPAL_ONE], tenant_account_id=other.tenant_account_id
        )
        with self.assertRaises(ValueError):
            ledger.register_billing_principal(
                TENANT_ONE, PRINCIPAL_ONE, "github_workflow", CATALOG_START
            )
        ledger.credential_records[CREDENTIAL_ONE] = replace(
            ledger.credential_records[CREDENTIAL_ONE], tenant_account_id=other.tenant_account_id
        )
        with self.assertRaises(ValueError):
            ledger.register_credential_record(
                TENANT_ONE, CREDENTIAL_ONE, "api_key", "fingerprint-one"
            )
        with self.assertRaises(ValueError):
            ledger.register_credential_assignment(
                TENANT_ONE, CREDENTIAL_ONE, PRINCIPAL_ONE, ACCOUNT_ONE, CATALOG_START
            )

    def test_credential_assignments_are_positive_and_non_overlapping(self) -> None:
        """Credential attribution accepts adjacent windows and rejects conflicts."""
        ledger = seed_ledger()
        credential_reference = "urn:cwl:tenant_001:credential_record:019d7004"
        ledger.register_credential_record(
            TENANT_ONE, credential_reference, "api_key", "fingerprint-adjacent"
        )
        window_end = CATALOG_START + timedelta(days=1)
        ledger.register_credential_assignment(
            TENANT_ONE,
            credential_reference,
            PRINCIPAL_ONE,
            ACCOUNT_ONE,
            CATALOG_START,
            window_end,
        )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            ledger.register_credential_assignment(
                TENANT_ONE,
                credential_reference,
                PRINCIPAL_ONE,
                ACCOUNT_ONE,
                window_end,
                window_end,
            )
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            ledger.register_credential_assignment(
                TENANT_ONE,
                credential_reference,
                PRINCIPAL_ONE,
                ACCOUNT_ONE,
                CATALOG_START + timedelta(hours=1),
                window_end + timedelta(hours=1),
            )
        adjacent = ledger.register_credential_assignment(
            TENANT_ONE,
            credential_reference,
            PRINCIPAL_ONE,
            ACCOUNT_ONE,
            window_end,
        )
        self.assertEqual(adjacent.valid_from, window_end)

    def test_resolve_methods_detect_corrupted_tenant_bindings(self) -> None:
        """Composite tenant checks still fire if a row is moved after registration."""
        ledger = seed_ledger()
        tenant = ledger.require_tenant(TENANT_ONE)
        other = ledger.require_tenant(TENANT_TWO)
        occurred_at = parse_iso8601_datetime("2026-08-16T10:27:42.482Z")
        ledger.billing_accounts[ACCOUNT_ONE] = replace(
            ledger.billing_accounts[ACCOUNT_ONE], tenant_account_id=other.tenant_account_id
        )
        _, account_error = ledger.resolve_billing_account(tenant, ACCOUNT_ONE)
        self.assertEqual(account_error, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH)
        ledger.billing_principals[PRINCIPAL_ONE] = replace(
            ledger.billing_principals[PRINCIPAL_ONE], tenant_account_id=other.tenant_account_id
        )
        _, principal_error = ledger.resolve_billing_principal(tenant, PRINCIPAL_ONE, occurred_at)
        self.assertEqual(principal_error, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH)
        ledger.credential_records[CREDENTIAL_ONE] = replace(
            ledger.credential_records[CREDENTIAL_ONE], tenant_account_id=other.tenant_account_id
        )
        restored_principal = replace(
            ledger.billing_principals[PRINCIPAL_ONE], tenant_account_id=tenant.tenant_account_id
        )
        restored_account = replace(
            ledger.billing_accounts[ACCOUNT_ONE], tenant_account_id=tenant.tenant_account_id
        )
        _, credential_error = ledger.resolve_credential(
            tenant, CREDENTIAL_ONE, restored_principal, restored_account, occurred_at
        )
        self.assertEqual(credential_error, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH)
        self.assertIsNone(ledger.find_by_source_event_key(tenant.tenant_account_id, "missing"))
        self.assertIsNone(
            ledger.find_by_payload_hash(tenant.tenant_account_id, "sha256:" + "b" * 64, 1)
        )
        self.assertIsNone(
            ledger.find_by_producer_event_id(
                tenant.tenant_account_id, UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf61c")
            )
        )

    def test_insert_is_immutable_and_meter_versions_select_highest(self) -> None:
        """A second insert of the same identity fails, and the newest meter wins."""
        service = UsageIngestionService(seed_ledger())
        event = make_event()
        receipt = service.ingest_usage_event(event)
        stored = service.ledger.usage_events[receipt.usage_event_id]
        with self.assertRaises(ValueError):
            service.ledger.insert_usage_event(stored)
        colliding_identity = replace(
            stored,
            usage_event_id=generate_record_id(),
            source_event_key="other_source_event",
            producer_event_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf999"),
        )
        with self.assertRaises(ValueError):
            service.ledger.insert_usage_event(colliding_identity)
        colliding_producer = replace(
            stored,
            usage_event_id=generate_record_id(),
            source_event_key="third_source_event",
            event_payload_hash="sha256:" + "e" * 64,
        )
        with self.assertRaises(ValueError):
            service.ledger.insert_usage_event(colliding_producer)
        colliding_source = replace(
            stored,
            usage_event_id=generate_record_id(),
            event_payload_hash="sha256:" + "f" * 64,
            producer_event_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bfbbb"),
        )
        with self.assertRaises(ValueError):
            service.ledger.insert_usage_event(colliding_source)
        newer = service.ledger.register_meter_definition(
            "gen_ai_output_token",
            2,
            "token",
            "sum",
            CATALOG_START,
        )
        service.ledger.register_meter_quality_rule(
            newer.meter_definition_id, "provider_reported", "billable"
        )
        definition, error = service.ledger.resolve_meter(
            "gen_ai_output_token",
            "token",
            "provider_reported",
            parse_iso8601_datetime("2026-08-16T10:27:42.482Z"),
        )
        self.assertIsNone(error)
        self.assertEqual(definition.meter_version, 2)
        expired = service.ledger.register_meter_definition(
            "legacy_request_count",
            1,
            "request",
            "sum",
            CATALOG_START,
            datetime(2026, 2, 1, tzinfo=UTC),
        )
        service.ledger.register_meter_quality_rule(
            expired.meter_definition_id, "provider_reported", "billable"
        )
        _, missing = service.ledger.resolve_meter(
            "legacy_request_count",
            "request",
            "provider_reported",
            parse_iso8601_datetime("2026-08-16T10:27:42.482Z"),
        )
        self.assertEqual(missing, RejectionReasonCode.METER_NOT_FOUND)

    def test_record_id_factory_and_exact_decimal_rules(self) -> None:
        """UUIDv7 is preferred, and decimal helpers reject inexact values."""
        uuid7_value = UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf61c")
        uuid4_value = UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf61d")
        with_v7 = SimpleNamespace(uuid7=lambda: uuid7_value, uuid4=lambda: uuid4_value)
        without_v7 = SimpleNamespace(uuid4=lambda: uuid4_value)
        self.assertEqual(generate_record_id(with_v7), uuid7_value)
        self.assertEqual(generate_record_id(without_v7), uuid4_value)
        self.assertIsInstance(generate_record_id(), UUID)

        self.assertEqual(parse_exact_decimal("1810.250"), Decimal("1810.250"))
        self.assertEqual(format_exact_decimal(Decimal("1810.250")), "1810.250")
        self.assertEqual(require_decimal_quantity(Decimal("2.5")), Decimal("2.5"))
        self.assertEqual(require_decimal_quantity("3"), Decimal("3"))
        with self.assertRaises(ExactDecimalError):
            parse_exact_decimal(1.25)  # type: ignore[arg-type]
        with self.assertRaises(ExactDecimalError):
            parse_exact_decimal("1e3")
        with self.assertRaises(ExactDecimalError):
            format_exact_decimal(1.25)  # type: ignore[arg-type]
        with self.assertRaises(ExactDecimalError):
            format_exact_decimal(Decimal("NaN"))
        with self.assertRaises(ExactDecimalError):
            format_exact_decimal(Decimal("Infinity"))
        with self.assertRaises(ExactDecimalError):
            format_exact_decimal(Decimal("-1"))

    def test_time_window_and_payload_hash_vectors(self) -> None:
        """ISO 8601 windows and the documented hash algorithm stay deterministic."""
        with self.assertRaises(TimeWindowError):
            parse_iso8601_datetime(2026)  # type: ignore[arg-type]
        with self.assertRaises(TimeWindowError):
            parse_iso8601_datetime("not-a-timestamp")
        with self.assertRaises(TimeWindowError):
            parse_iso8601_datetime("2026-08-16T10:27:42")
        parsed = parse_iso8601_datetime("2026-08-16T10:27:42.482Z")
        self.assertEqual(parsed.tzinfo, UTC)
        with self.assertRaises(TimeWindowError):
            TimeWindow(datetime(2026, 8, 16), datetime(2026, 8, 17, tzinfo=UTC))
        with self.assertRaises(TimeWindowError):
            TimeWindow(datetime(2026, 8, 16, tzinfo=UTC), datetime(2026, 8, 17))
        with self.assertRaises(TimeWindowError):
            TimeWindow(
                datetime(2026, 8, 17, tzinfo=UTC),
                datetime(2026, 8, 16, tzinfo=UTC),
            )
        window = TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z")
        self.assertTrue(window.contains(parsed))
        self.assertFalse(window.contains(parse_iso8601_datetime("2026-08-16T11:00:00Z")))
        with self.assertRaises(TimeWindowError):
            window.contains(datetime(2026, 8, 16, 10, 30))

        event = make_event()
        canonical = json.dumps(
            canonical_source_payload(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(compute_source_payload_hash(event), expected)
        self.assertEqual(source_payload_hash_errors(event), ())
        self.assertTrue(source_payload_hash_errors(make_event(source_payload_hash="sha256:" + "c" * 64)))
        raw_payload = canonical_source_payload({"occurred_at": "2026-08-16T10:27:42.482Z", "measurements": ["skip"]})
        self.assertEqual(raw_payload["measurements"], ["skip"])

    def test_receipt_semantics_require_ids_reasons_and_matching_counts(self) -> None:
        """A receipt contract is invalid when evidence or counts drift."""
        valid = {
            "batch_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "receipt_contract_version": 1,
            "accepted_event_count": 1,
            "duplicate_replay_count": 0,
            "rejected_event_count": 0,
            "event_receipts": [
                {
                    "source_event_key": "workflow_381:step_04:attempt_01",
                    "ingestion_outcome_code": "accepted",
                    "usage_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
                    "event_contract_version": 1,
                    "source_payload_hash": "sha256:" + "d" * 64,
                }
            ],
        }
        self.assertEqual(validate_usage_ingestion_receipt(valid), ())
        missing_id = json.loads(json.dumps(valid))
        del missing_id["event_receipts"][0]["usage_event_id"]
        self.assertIn(
            "$: accepted receipts must include usage_event_id",
            validate_usage_ingestion_receipt(missing_id),
        )
        missing_version = json.loads(json.dumps(valid))
        del missing_version["event_receipts"][0]["event_contract_version"]
        self.assertIn(
            "$: accepted receipts must include event_contract_version",
            validate_usage_ingestion_receipt(missing_version),
        )
        missing_hash = json.loads(json.dumps(valid))
        del missing_hash["event_receipts"][0]["source_payload_hash"]
        self.assertIn(
            "$: accepted receipts must include source_payload_hash",
            validate_usage_ingestion_receipt(missing_hash),
        )
        replay = json.loads(json.dumps(valid))
        replay["accepted_event_count"] = 0
        replay["duplicate_replay_count"] = 1
        replay["event_receipts"][0]["ingestion_outcome_code"] = "duplicate_replay"
        del replay["event_receipts"][0]["usage_event_id"]
        del replay["event_receipts"][0]["event_contract_version"]
        del replay["event_receipts"][0]["source_payload_hash"]
        replay_errors = validate_usage_ingestion_receipt(replay)
        self.assertIn(
            "$: duplicate_replay receipts must include usage_event_id",
            replay_errors,
        )
        self.assertIn(
            "$: duplicate_replay receipts must include event_contract_version",
            replay_errors,
        )
        self.assertIn(
            "$: duplicate_replay receipts must include source_payload_hash",
            replay_errors,
        )
        rejected = json.loads(json.dumps(valid))
        rejected["accepted_event_count"] = 0
        rejected["rejected_event_count"] = 1
        rejected["event_receipts"][0]["ingestion_outcome_code"] = "rejected"
        del rejected["event_receipts"][0]["usage_event_id"]
        self.assertIn(
            "$: rejected receipts must include rejection_reason_code",
            validate_usage_ingestion_receipt(rejected),
        )
        mismatched = json.loads(json.dumps(valid))
        mismatched["accepted_event_count"] = 4
        self.assertIn(
            "$: accepted_event_count must match event_receipts",
            validate_usage_ingestion_receipt(mismatched),
        )
        mismatched_replay = json.loads(json.dumps(valid))
        mismatched_replay["event_receipts"][0]["ingestion_outcome_code"] = "duplicate_replay"
        self.assertIn(
            "$: duplicate_replay_count must match event_receipts",
            validate_usage_ingestion_receipt(mismatched_replay),
        )
        mismatched_rejected = json.loads(json.dumps(valid))
        mismatched_rejected["event_receipts"][0]["ingestion_outcome_code"] = "rejected"
        mismatched_rejected["event_receipts"][0]["rejection_reason_code"] = "schema_invalid"
        del mismatched_rejected["event_receipts"][0]["usage_event_id"]
        self.assertIn(
            "$: rejected_event_count must match event_receipts",
            validate_usage_ingestion_receipt(mismatched_rejected),
        )
        self.assertTrue(
            validate_usage_ingestion_receipt({"batch_receipt_id": 1})
        )
        self.assertTrue(validate_usage_ingestion_receipt(["not-an-object"]))
        malformed_items = json.loads(json.dumps(valid))
        malformed_items["event_receipts"] = ["not-an-object"]
        malformed_items["accepted_event_count"] = 0
        self.assertTrue(validate_usage_ingestion_receipt(malformed_items))
        unknown_outcome = json.loads(json.dumps(valid))
        unknown_outcome["event_receipts"] = [
            {
                "source_event_key": "mystery_event",
                "ingestion_outcome_code": "mystery",
            },
            unknown_outcome["event_receipts"][0],
        ]
        self.assertTrue(validate_usage_ingestion_receipt(unknown_outcome))
        self.assertEqual(validate_schema_instance({"type": "object"}, {}), ())

    def test_schema_loader_rejects_missing_and_non_object_contracts(self) -> None:
        """Importers get an actionable error when a contract file is unusable."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            with self.assertRaises(FileNotFoundError):
                load_json_schema("missing.schema.json", directory)
            (directory / "empty.schema.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_json_schema("empty.schema.json", directory)

    def test_identity_extraction_covers_non_string_optional_fields(self) -> None:
        """Receipt identity falls back when optional fields have the wrong type."""
        service = UsageIngestionService()
        receipt = service.ingest_usage_event(
            {
                "source_event_key": 7,
                "event_contract_version": "1",
                "source_payload_hash": 9,
                "tenant_reference": 3,
            }
        )
        self.assertEqual(receipt.source_event_key, "unavailable_source_event_key")
        self.assertIsNone(receipt.event_contract_version)
        self.assertIsNone(receipt.source_payload_hash)
        self.assertIsNone(receipt.tenant_reference)


if __name__ == "__main__":
    unittest.main()
