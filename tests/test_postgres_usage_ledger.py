"""Real PostgreSQL integration tests for the durable usage repository."""

from __future__ import annotations

import os
from threading import Barrier
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from metering_billing import PostgresUsageLedger, UsageIngestionService
from metering_billing.errors import RejectionReasonCode, UsageEventConflict
from tests.test_usage_ingestion import (
    ACCOUNT_ONE,
    ACCOUNT_TWO,
    CATALOG_START,
    CREDENTIAL_ONE,
    CREDENTIAL_TWO,
    PRINCIPAL_ONE,
    PRINCIPAL_TWO,
    TENANT_ONE,
    TENANT_TWO,
    make_event,
)


POSTGRES_DSN = os.environ.get(
    "METERING_BILLING_POSTGRES_DSN", "dbname=metering_billing_usage_repo_test"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PostgresUsageLedgerTests(unittest.TestCase):
    """Exercise catalog, transaction, retry, and tenant behavior against PostgreSQL."""

    @classmethod
    def setUpClass(cls) -> None:
        """Install the checked-in migration set in the dedicated test database."""
        if "test" not in POSTGRES_DSN.lower():
            raise RuntimeError(
                "METERING_BILLING_POSTGRES_DSN must point to a dedicated test database"
            )
        cls.connection = psycopg.connect(POSTGRES_DSN)
        cls.connection.execute("DROP SCHEMA IF EXISTS billing_core CASCADE")
        cls.connection.commit()
        migration_directory = os.path.join(ROOT, "database", "migrations")
        for name in sorted(os.listdir(migration_directory)):
            if not name.endswith(".sql"):
                continue
            with open(os.path.join(migration_directory, name), encoding="utf-8") as migration:
                cls.connection.execute(migration.read())
        cls.connection.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the dedicated integration connection."""
        cls.connection.close()

    def setUp(self) -> None:
        """Reset only the dedicated test database tables and seed two tenants."""
        self.connection.execute(
            """
            TRUNCATE TABLE
                billing_core.usage_ingestion_receipt,
                billing_core.usage_measurement,
                billing_core.usage_event,
                billing_core.credential_assignment,
                billing_core.credential_record,
                billing_core.billing_principal,
                billing_core.billing_account,
                billing_core.meter_quality_rule,
                billing_core.meter_definition,
                billing_core.tenant_account
            RESTART IDENTITY CASCADE
            """
        )
        self.connection.commit()
        self.ledger = PostgresUsageLedger(self.connection)
        for tenant, account, principal, credential in (
            (TENANT_ONE, ACCOUNT_ONE, PRINCIPAL_ONE, CREDENTIAL_ONE),
            (TENANT_TWO, ACCOUNT_TWO, PRINCIPAL_TWO, CREDENTIAL_TWO),
        ):
            self.ledger.register_tenant(tenant)
            self.ledger.register_billing_account(tenant, account)
            self.ledger.register_billing_principal(
                tenant, principal, "github_workflow", CATALOG_START
            )
            self.ledger.register_credential_record(tenant, credential, "api_key", credential)
            self.ledger.register_credential_assignment(
                tenant, credential, principal, account, CATALOG_START
            )
        self.meter = self.ledger.register_meter_definition(
            "gen_ai_output_token", 1, "token", "sum", CATALOG_START
        )
        self.ledger.register_meter_quality_rule(
            self.meter.meter_definition_id, "provider_reported", "billable"
        )

    def tearDown(self) -> None:
        """Rollback an accidental open transaction from a failed assertion."""
        self.connection.rollback()

    def test_registration_is_idempotent_and_resolvers_fail_closed(self) -> None:
        """Repeated catalog writes are stable and every resolver preserves tenant boundaries."""
        tenant = self.ledger.register_tenant(TENANT_ONE)
        self.assertEqual(self.ledger.register_tenant(TENANT_ONE), tenant)
        account = self.ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE)
        self.assertEqual(self.ledger.register_billing_account(TENANT_ONE, ACCOUNT_ONE), account)
        principal = self.ledger.register_billing_principal(
            TENANT_ONE, PRINCIPAL_ONE, "github_workflow", CATALOG_START
        )
        self.assertEqual(
            self.ledger.register_billing_principal(
                TENANT_ONE, PRINCIPAL_ONE, "github_workflow", CATALOG_START
            ),
            principal,
        )
        self.ledger.register_billing_principal(
            TENANT_ONE,
            PRINCIPAL_ONE,
            "github_workflow",
            CATALOG_START + timedelta(days=1),
        )
        self.assertEqual(
            self.ledger.resolve_billing_principal(
                tenant, PRINCIPAL_ONE, CATALOG_START
            )[0],
            principal,
        )
        credential = self.ledger.register_credential_record(
            TENANT_ONE, CREDENTIAL_ONE, "api_key", CREDENTIAL_ONE
        )
        self.assertEqual(
            self.ledger.register_credential_record(
                TENANT_ONE, CREDENTIAL_ONE, "api_key", CREDENTIAL_ONE
            ),
            credential,
        )
        self.assertEqual(self.ledger.resolve_tenant(TENANT_ONE)[0], tenant)
        self.assertEqual(
            self.ledger.resolve_tenant("urn:cwl:missing_tenant")[1],
            RejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            self.ledger.resolve_billing_account(tenant, ACCOUNT_TWO)[1],
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )
        self.assertEqual(
            self.ledger.resolve_billing_principal(tenant, PRINCIPAL_TWO, CATALOG_START)[1],
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )
        self.assertEqual(
            self.ledger.resolve_billing_account(tenant, "urn:cwl:tenant_001:missing_account")[1],
            RejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND,
        )
        self.ledger.register_billing_account(
            TENANT_ONE, "urn:cwl:tenant_001:billing_account:suspended", "suspended"
        )
        self.assertEqual(
            self.ledger.resolve_billing_account(
                tenant, "urn:cwl:tenant_001:billing_account:suspended"
            )[1],
            RejectionReasonCode.BILLING_ACCOUNT_NOT_ACTIVE,
        )
        self.assertEqual(
            self.ledger.resolve_billing_principal(
                tenant, PRINCIPAL_ONE, CATALOG_START - timedelta(seconds=1)
            )[1],
            RejectionReasonCode.PRINCIPAL_NOT_EFFECTIVE,
        )
        self.assertEqual(
            self.ledger.resolve_billing_principal(
                tenant, "urn:cwl:tenant_001:missing_principal", CATALOG_START
            )[1],
            RejectionReasonCode.BILLING_PRINCIPAL_NOT_FOUND,
        )
        self.assertEqual(
            self.ledger.resolve_credential(
                tenant,
                CREDENTIAL_TWO,
                principal,
                account,
                CATALOG_START,
            )[1],
            RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH,
        )
        self.assertEqual(
            self.ledger.resolve_credential(
                tenant,
                "urn:cwl:tenant_001:credential_record:missing",
                principal,
                account,
                CATALOG_START,
            )[1],
            RejectionReasonCode.CREDENTIAL_NOT_FOUND,
        )
        unassigned = "urn:cwl:tenant_001:credential_record:unassigned"
        self.ledger.register_credential_record(tenant.tenant_reference, unassigned, "api_key", unassigned)
        self.assertEqual(
            self.ledger.resolve_credential(tenant, unassigned, principal, account, CATALOG_START)[1],
            RejectionReasonCode.CREDENTIAL_NOT_ASSIGNED,
        )
        with self.assertRaises(KeyError):
            self.ledger.require_tenant("urn:cwl:missing_tenant")

    def test_meter_and_assignment_effective_rules(self) -> None:
        """Half-open assignment and meter quality rules match the reference semantics."""
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self.ledger.register_credential_assignment(
                TENANT_ONE,
                CREDENTIAL_ONE,
                PRINCIPAL_ONE,
                ACCOUNT_ONE,
                CATALOG_START,
                CATALOG_START,
            )
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            self.ledger.register_credential_assignment(
                TENANT_ONE,
                CREDENTIAL_ONE,
                PRINCIPAL_ONE,
                ACCOUNT_ONE,
                CATALOG_START + timedelta(hours=1),
            )
        self.assertEqual(
            self.ledger.resolve_meter(
                "missing_meter", "token", "provider_reported", CATALOG_START
            )[1],
            RejectionReasonCode.METER_NOT_FOUND,
        )
        self.assertEqual(
            self.ledger.resolve_meter(
                "gen_ai_output_token", "byte", "provider_reported", CATALOG_START
            )[1],
            RejectionReasonCode.METER_UNIT_MISMATCH,
        )
        self.assertEqual(
            self.ledger.resolve_meter(
                "gen_ai_output_token", "token", "estimated", CATALOG_START
            )[1],
            RejectionReasonCode.METER_QUALITY_NOT_ALLOWED,
        )
        self.assertEqual(
            self.ledger.register_meter_quality_rule(
                self.meter.meter_definition_id, "provider_reported", "billable"
            ).meter_definition_id,
            self.meter.meter_definition_id,
        )
        with self.assertRaises(KeyError):
            self.ledger.register_credential_assignment(
                TENANT_ONE,
                CREDENTIAL_ONE,
                "urn:cwl:tenant_001:billing_principal:missing",
                ACCOUNT_ONE,
                CATALOG_START,
            )
        with self.assertRaises(KeyError):
            self.ledger.register_credential_assignment(
                TENANT_ONE,
                "urn:cwl:tenant_001:credential_record:missing",
                PRINCIPAL_ONE,
                ACCOUNT_ONE,
                CATALOG_START,
            )
        with self.assertRaises(KeyError):
            self.ledger.register_credential_assignment(
                TENANT_ONE,
                CREDENTIAL_ONE,
                PRINCIPAL_ONE,
                "urn:cwl:tenant_001:billing_account:missing",
                CATALOG_START,
            )
        meter = self.ledger.register_meter_definition(
            "gen_ai_output_token", 1, "token", "sum", CATALOG_START
        )
        self.assertEqual(meter, self.meter)

    def test_ingestion_is_atomic_replay_safe_and_tenant_scoped(self) -> None:
        """A restart-safe repository keeps one event, normalized measurements, and receipts."""
        service = UsageIngestionService(self.ledger)
        event = make_event()
        accepted = service.ingest_usage_event(event)
        replay = service.ingest_usage_event(event)
        self.assertEqual(accepted.ingestion_outcome_code.value, "accepted")
        self.assertEqual(replay.ingestion_outcome_code.value, "duplicate_replay")
        self.assertEqual(accepted.usage_event_id, replay.usage_event_id)
        self.assertEqual(len(self.ledger.list_usage_events()), 1)
        self.assertEqual(len(self.ledger.list_ingestion_receipts()), 2)
        self.assertEqual(
            len(self.ledger.list_ingestion_receipts(self.ledger.require_tenant(TENANT_ONE).tenant_account_id)),
            2,
        )
        stored = self.ledger.get_usage_event(accepted.usage_event_id)
        self.assertIsNotNone(stored)
        self.assertEqual(len(stored.measurements), 1)
        self.assertEqual(len(self.ledger.stored_usage_set(self.ledger.require_tenant(TENANT_ONE).tenant_account_id)), 1)
        self.assertEqual(
            len(self.ledger.list_usage_events(self.ledger.require_tenant(TENANT_TWO).tenant_account_id)),
            0,
        )
        self.assertIsNone(self.ledger.get_usage_event(uuid4()))
        self.assertEqual(self.ledger.list_usage_events_in_window(
            self.ledger.require_tenant(TENANT_ONE).tenant_account_id,
            datetime(2026, 8, 16, tzinfo=UTC),
            datetime(2026, 8, 17, tzinfo=UTC),
        )[0].usage_event_id, accepted.usage_event_id)

    def test_service_conflict_reasons_are_deterministic(self) -> None:
        """Source, payload, and producer identities reject different facts."""
        service = UsageIngestionService(self.ledger)
        first = service.ingest_usage_event(make_event())
        source_conflict = service.ingest_usage_event(
            make_event(operation_code="changed", source_event_key="workflow_381:step_04:attempt_01")
        )
        payload_conflict = service.ingest_usage_event(
            make_event(source_event_key="different:source:key")
        )
        producer_conflict = service.ingest_usage_event(
            make_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
                source_event_key="different:producer:key",
                operation_code="different_producer_payload",
            )
        )
        self.assertEqual(first.ingestion_outcome_code.value, "accepted")
        self.assertEqual(source_conflict.rejection_reason_code, RejectionReasonCode.SOURCE_EVENT_CONFLICT)
        self.assertEqual(payload_conflict.rejection_reason_code, RejectionReasonCode.PAYLOAD_HASH_CONFLICT)
        self.assertEqual(producer_conflict.rejection_reason_code, RejectionReasonCode.PRODUCER_EVENT_CONFLICT)

    def test_direct_database_conflicts_are_classified(self) -> None:
        """The insert path classifies a race even when pre-checks did not observe it."""
        service = UsageIngestionService(self.ledger)
        accepted = service.ingest_usage_event(make_event())
        stored = self.ledger.get_usage_event(accepted.usage_event_id)
        assert stored is not None
        with self.assertRaises(UsageEventConflict) as replay_error:
            self.ledger.insert_usage_event(replace(stored, usage_event_id=uuid4()))
        self.assertTrue(replay_error.exception.duplicate_replay)
        with self.assertRaises(UsageEventConflict) as source_error:
            self.ledger.insert_usage_event(
                replace(stored, usage_event_id=uuid4(), event_payload_hash="sha256:" + "a" * 64)
            )
        self.assertEqual(
            source_error.exception.rejection_reason_code,
            RejectionReasonCode.SOURCE_EVENT_CONFLICT,
        )
        with self.assertRaises(UsageEventConflict) as payload_error:
            self.ledger.insert_usage_event(
                replace(stored, usage_event_id=uuid4(), source_event_key="payload:direct", producer_event_id=uuid4())
            )
        self.assertEqual(
            payload_error.exception.rejection_reason_code,
            RejectionReasonCode.PAYLOAD_HASH_CONFLICT,
        )
        with self.assertRaises(UsageEventConflict) as producer_error:
            self.ledger.insert_usage_event(
                replace(
                    stored,
                    usage_event_id=uuid4(),
                    source_event_key="producer:direct",
                    event_payload_hash="sha256:" + "b" * 64,
                )
            )
        self.assertEqual(
            producer_error.exception.rejection_reason_code,
            RejectionReasonCode.PRODUCER_EVENT_CONFLICT,
        )
        with self.assertRaisesRegex(ValueError, "no classified existing row"):
            self.ledger.insert_usage_event(
                replace(
                    stored,
                    producer_event_id=uuid4(),
                    source_event_key="event-id-only-conflict",
                    event_payload_hash="sha256:" + "d" * 64,
                )
            )
        with self.connection.cursor() as cursor:
            with self.assertRaises(KeyError):
                self.ledger._fetch_usage_event(cursor, uuid4())

    def test_measurement_failure_rolls_back_event(self) -> None:
        """A failed normalized measurement never leaves a parent usage event behind."""
        service = UsageIngestionService(self.ledger)
        accepted = service.ingest_usage_event(make_event())
        stored = self.ledger.get_usage_event(accepted.usage_event_id)
        assert stored is not None
        bad_measurement = replace(
            stored.measurements[0],
            usage_measurement_id=uuid4(),
            meter_definition_id=uuid4(),
        )
        bad_event = replace(
            stored,
            usage_event_id=uuid4(),
            producer_event_id=uuid4(),
            source_event_key="rollback:measurement",
            event_payload_hash="sha256:" + "c" * 64,
            measurements=(bad_measurement,),
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.ledger.insert_usage_event(bad_event)
        self.assertIsNone(self.ledger.get_usage_event(bad_event.usage_event_id))

    def test_concurrent_duplicate_requests_have_one_effect(self) -> None:
        """Eight simultaneous identical requests produce one event and eight audit receipts."""
        event = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf622",
            source_event_key="concurrent:source:01",
            operation_code="concurrent_write",
        )
        barrier = Barrier(8)

        class BarrierLedger(PostgresUsageLedger):
            def find_by_source_event_key(self, tenant_account_id, source_event_key):
                result = super().find_by_source_event_key(tenant_account_id, source_event_key)
                barrier.wait()
                return result

        def ingest_once(_: int) -> str:
            with psycopg.connect(POSTGRES_DSN) as connection:
                receipt = UsageIngestionService(BarrierLedger(connection)).ingest_usage_event(event)
                return receipt.ingestion_outcome_code.value

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(ingest_once, range(8)))
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("duplicate_replay"), 7, outcomes)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM billing_core.usage_event WHERE source_event_key = %s",
                (event["source_event_key"],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM billing_core.usage_ingestion_receipt WHERE source_event_key = %s",
                (event["source_event_key"],),
            ).fetchone()[0],
            8,
        )

    def test_concurrent_source_conflict_is_recorded(self) -> None:
        """A raced changed fact becomes a rejected receipt after the unique-key wait."""
        barrier = Barrier(2)

        class BarrierLedger(PostgresUsageLedger):
            def find_by_source_event_key(self, tenant_account_id, source_event_key):
                result = super().find_by_source_event_key(tenant_account_id, source_event_key)
                barrier.wait()
                return result

        events = (
            make_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf623",
                source_event_key="concurrent:source:conflict",
                operation_code="first_write",
            ),
            make_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf624",
                source_event_key="concurrent:source:conflict",
                operation_code="changed_write",
            ),
        )

        def ingest_once(event):
            with psycopg.connect(POSTGRES_DSN) as connection:
                return UsageIngestionService(BarrierLedger(connection)).ingest_usage_event(event)

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(ingest_once, events))
        self.assertEqual(
            sorted(receipt.ingestion_outcome_code.value for receipt in receipts),
            ["accepted", "rejected"],
        )
        rejected = next(receipt for receipt in receipts if receipt.ingestion_outcome_code.value == "rejected")
        self.assertEqual(rejected.rejection_reason_code, RejectionReasonCode.SOURCE_EVENT_CONFLICT)

    def test_transaction_context_and_connection_lifecycle(self) -> None:
        """The outer ingestion transaction avoids a partial receipt and owned connections close."""
        with self.ledger.ingestion_transaction():
            with self.ledger.ingestion_transaction():
                self.ledger.register_tenant("urn:cwl:tenant_transaction")
        self.assertIsNotNone(self.ledger.require_tenant("urn:cwl:tenant_transaction"))
        self.ledger.close()
        owned = PostgresUsageLedger.connect(POSTGRES_DSN)
        owned.close()
        self.assertIsNone(self.ledger.get_usage_event(uuid4()))


if __name__ == "__main__":
    unittest.main()
