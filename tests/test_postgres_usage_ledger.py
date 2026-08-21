"""Real PostgreSQL integration tests for the durable usage repository."""

from __future__ import annotations

import os
from threading import Barrier
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg

from metering_billing import (
    InvoiceDraftService,
    IssuedInvoiceService,
    PostgresUsageLedger,
    UsageIngestionService,
)
from metering_billing.errors import RejectionReasonCode, UsageEventConflict
from metering_billing.rate_card import RateCardService
from metering_billing.time_window import TimeWindow
from metering_billing.usage_rating import UsageRatingService
from scripts.migrate_postgres import (
    MIGRATION_HISTORY_TABLE,
    MigrationDriftError,
    MigrationPlanError,
    apply_migrations,
    main as migrate_main,
)
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
        cls.connection.execute(f"DROP TABLE IF EXISTS {MIGRATION_HISTORY_TABLE}")
        cls.connection.execute("DROP SCHEMA IF EXISTS billing_core CASCADE")
        cls.connection.commit()
        migration_directory = Path(ROOT) / "database" / "migrations"
        applied = apply_migrations(cls.connection, migration_directory)
        if len(applied) != 38:
            raise AssertionError(f"expected 38 migrations, got {len(applied)}")

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the dedicated integration connection."""
        cls.connection.close()

    def setUp(self) -> None:
        """Reset only the dedicated test database tables and seed two tenants."""
        self.connection.execute(
            """
            TRUNCATE TABLE
                billing_core.webhook_outbox_event,
                billing_core.issued_invoice_line,
                billing_core.issued_invoice,
                billing_core.invoice_draft_line,
                billing_core.invoice_draft,
                billing_core.rating_line,
                billing_core.rating_run,
                billing_core.rate_card_line,
                billing_core.rate_card_version,
                billing_core.rate_card,
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

    def test_migration_runner_is_idempotent_and_detects_drift(self) -> None:
        """The runner records checksums and refuses a changed applied migration."""
        migration_directory = Path(ROOT) / "database" / "migrations"
        self.assertEqual(apply_migrations(self.connection, migration_directory), ())
        first_name = "0001_initial_billing_core.sql"
        original_checksum = self.connection.execute(
            f"SELECT checksum_sha256 FROM {MIGRATION_HISTORY_TABLE} WHERE migration_name = %s",
            (first_name,),
        ).fetchone()[0]
        self.connection.execute(
            f"UPDATE {MIGRATION_HISTORY_TABLE} SET checksum_sha256 = %s WHERE migration_name = %s",
            ("bad", first_name),
        )
        self.connection.commit()
        with self.assertRaises(MigrationDriftError):
            apply_migrations(self.connection, migration_directory)
        self.connection.execute(
            f"UPDATE {MIGRATION_HISTORY_TABLE} SET checksum_sha256 = %s WHERE migration_name = %s",
            (original_checksum, first_name),
        )
        self.connection.commit()
        self.assertEqual(
            migrate_main(["--dsn", POSTGRES_DSN, "--migrations", str(migration_directory)]),
            0,
        )

    def test_migration_runner_rejects_bad_plans(self) -> None:
        """Migration names and transaction wrappers are explicit contracts."""
        with TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(MigrationPlanError):
                apply_migrations(self.connection, path)
            invalid = path / "0001_bad-name.sql"
            invalid.write_text("BEGIN; SELECT 1; COMMIT;", encoding="utf-8")
            with self.assertRaises(MigrationPlanError):
                apply_migrations(self.connection, path)
            invalid.unlink()
            valid = path / "0001_valid_name.sql"
            valid.write_text("BEGIN; COMMIT;", encoding="utf-8")
            with self.assertRaises(MigrationPlanError):
                apply_migrations(self.connection, path)
            valid.write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaises(MigrationPlanError):
                apply_migrations(self.connection, path)

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

    def test_rate_card_and_rating_are_durable_and_tenant_scoped(self) -> None:
        """The first usage-to-rating path survives reload and exact replay."""
        ingest = UsageIngestionService(self.ledger)
        accepted = ingest.ingest_usage_event(make_event())
        self.assertEqual(accepted.ingestion_outcome_code.value, "accepted")
        card_lines = (
            {
                "metric_code": "gen_ai_output_token",
                "unit_amount": "0.000002",
                "currency_code": "USD",
            },
        )
        first_card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE, "cwl_standard", "USD", card_lines
        )
        second_card = RateCardService(self.ledger).publish_rate_card(
            TENANT_TWO, "cwl_standard", "USD", card_lines
        )
        self.assertNotEqual(first_card.rate_card_id, second_card.rate_card_id)
        tenant_one_id = self.ledger.require_tenant(TENANT_ONE).tenant_account_id
        self.assertEqual(len(self.ledger.list_rate_cards(tenant_one_id)), 1)
        first_version = self.ledger.get_rate_card_version(first_card.rate_card_version_id)
        self.assertIsNotNone(first_version)
        assert first_version is not None
        self.assertEqual(
            self.ledger.find_rate_card_version_by_identity(
                tenant_one_id,
                first_card.rate_card_id,
                first_version.source_payload_hash,
                first_version.rate_card_contract_version,
            ),
            first_version,
        )
        self.assertEqual(
            self.ledger.find_rate_card_version(tenant_one_id, 1, "cwl_standard"),
            first_version,
        )
        self.assertIsNone(self.ledger.find_rate_card_version(tenant_one_id, 99))
        self.assertIsNone(self.ledger.get_rate_card_version(uuid4()))
        self.assertEqual(
            self.ledger.list_rate_card_versions(tenant_one_id), (first_version,)
        )
        self.assertEqual(
            self.ledger.list_rate_card_versions(tenant_one_id, first_card.rate_card_id),
            (first_version,),
        )
        self.assertEqual(self.ledger.insert_rate_card_version(first_version), first_version)
        stored_card = self.ledger.get_rate_card(first_card.rate_card_id)
        self.assertIsNotNone(stored_card)
        assert stored_card is not None
        with self.assertRaises(ValueError):
            self.ledger.insert_rate_card(replace(stored_card, currency_code="EUR"))
        with self.ledger.transaction():
            with self.ledger.transaction():
                pass
        self.assertIsNone(self.ledger.find_meter_quality_rule(self.meter.meter_definition_id, "missing"))
        with self.assertRaises(KeyError):
            self.ledger.billing_account_reference_for(uuid4())
        window = TimeWindow(
            datetime(2026, 8, 16, 10, tzinfo=UTC),
            datetime(2026, 8, 16, 12, tzinfo=UTC),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(rating.rating_outcome_code.value, "accepted")
        self.assertEqual(rating.rated_total_amount, Decimal("0.003620"))
        replay = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(replay.rating_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.rating_run_id, rating.rating_run_id)
        stored = self.ledger.get_rating_run(rating.rating_run_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(self.ledger.list_rating_runs(), (stored,))
        self.assertEqual(
            self.ledger.find_rating_run(
                tenant_one_id,
                stored.window_started_at,
                stored.window_ended_at,
                stored.rate_card_id,
                stored.usage_snapshot_hash,
            ),
            stored,
        )
        self.assertEqual(self.ledger.insert_rating_run(stored, stored.rating_lines), stored)
        self.assertEqual(stored.rate_card_id, first_card.rate_card_id)
        self.assertEqual(stored.rating_lines[0].billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(stored.rating_lines[0].meter_code, "gen_ai_output_token")
        self.assertEqual(self.ledger.find_rate_card_line(first_card.rate_card_version_id, "missing"), None)
        self.assertEqual(len(self.ledger.list_rating_runs(self.ledger.require_tenant(TENANT_ONE).tenant_account_id)), 1)
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft.invoice_draft_outcome_code.value, "accepted")
        draft_replay = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        self.assertEqual(draft_replay.invoice_draft_outcome_code.value, "duplicate_replay")
        self.assertEqual(draft_replay.invoice_draft_id, draft.invoice_draft_id)
        self.assertEqual(draft.drafted_total_amount, Decimal("0.003620"))
        stored_draft = self.ledger.get_invoice_draft(draft.invoice_draft_id)
        self.assertIsNotNone(stored_draft)
        assert stored_draft is not None
        self.assertEqual(
            self.ledger.insert_invoice_draft(stored_draft, stored_draft.invoice_draft_lines),
            stored_draft,
        )
        self.assertEqual(stored_draft.invoice_draft_lines[0].billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(
            InvoiceDraftService(self.ledger).draft_invoice(TENANT_TWO, rating.rating_run_id).invoice_draft_outcome_code.value,
            "rejected",
        )
        self.assertEqual(len(self.ledger.list_invoice_drafts(self.ledger.require_tenant(TENANT_ONE).tenant_account_id)), 1)
        issued = IssuedInvoiceService(self.ledger, clock=lambda: datetime(2026, 8, 17, tzinfo=UTC)).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id, "2026-08-31T00:00:00Z"
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        issued_replay = IssuedInvoiceService(self.ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id, "2026-09-30T00:00:00Z"
        )
        self.assertEqual(issued_replay.issued_invoice_outcome_code.value, "duplicate_replay")
        self.assertEqual(issued_replay.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(issued.tax_inclusive_amount, Decimal("0.003620"))
        stored_issued = self.ledger.get_issued_invoice(issued.issued_invoice_id)
        self.assertIsNotNone(stored_issued)
        assert stored_issued is not None
        self.assertEqual(
            self.ledger.list_issued_invoices_for_tenant(tenant_one_id), (stored_issued,)
        )
        self.assertEqual(
            self.ledger.insert_issued_invoice(
                stored_issued, stored_issued.issued_invoice_lines
            ),
            stored_issued,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice(
                replace(stored_issued, invoice_draft_id=uuid4()), ()
            )
        self.assertEqual(stored_issued.issued_invoice_lines[0].billing_account_reference, ACCOUNT_ONE)
        outbox_events = self.ledger.list_webhook_outbox_events_for_tenant(tenant_one_id)
        self.assertEqual(len(outbox_events), 1)
        self.assertEqual(self.ledger.get_webhook_outbox_event(outbox_events[0].outbox_event_id), outbox_events[0])
        self.assertEqual(self.ledger.list_pending_webhook_outbox_events(tenant_one_id), outbox_events)
        self.assertEqual(self.ledger.insert_webhook_outbox_event(outbox_events[0]), outbox_events[0])
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_outbox_event(
                replace(outbox_events[0], payload_hash="sha256:" + "2" * 64)
            )
        self.assertEqual(
            IssuedInvoiceService(self.ledger).issue_invoice(TENANT_TWO, draft.invoice_draft_id).issued_invoice_outcome_code.value,
            "rejected",
        )

    def test_issued_invoice_reads_a_durable_tax_snapshot(self) -> None:
        """Issued totals use the same tenant-scoped PostgreSQL tax snapshot."""
        ingest = UsageIngestionService(self.ledger)
        self.assertEqual(ingest.ingest_usage_event(make_event()).ingestion_outcome_code.value, "accepted")
        card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            ({"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(datetime(2026, 8, 16, 10, tzinfo=UTC), datetime(2026, 8, 16, 12, tzinfo=UTC)),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        tenant_id = self.ledger.require_tenant(TENANT_ONE).tenant_account_id
        schedule_id, version_id, assessment_id = uuid4(), uuid4(), uuid4()
        snapshot_hash = "sha256:" + "1" * 64
        self.connection.execute(
            """
            INSERT INTO billing_core.tax_rate_schedule
                (tax_rate_schedule_id, tenant_account_id, tax_code)
            VALUES (%s, %s, 'vat')
            """,
            (schedule_id, tenant_id),
        )
        self.connection.execute(
            """
            INSERT INTO billing_core.tax_rate_version
                (tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                 version_number, tax_rate_contract_version, tax_code, tax_rate,
                 source_payload_hash)
            VALUES (%s, %s, %s, 1, 1, 'vat', 0.1, %s)
            """,
            (version_id, tenant_id, schedule_id, snapshot_hash),
        )
        self.connection.execute(
            """
            INSERT INTO billing_core.tax_assessment
                (tax_assessment_id, tenant_account_id, invoice_draft_id,
                 tax_rate_version_id, tax_assessment_contract_version, tax_code,
                 tax_rate, currency_code, tax_exclusive_amount, tax_amount,
                 tax_inclusive_amount, source_payload_hash)
            VALUES (%s, %s, %s, %s, 1, 'vat', 0.1, 'USD', 0.003620, 0.000362, 0.003982, %s)
            """,
            (assessment_id, tenant_id, draft.invoice_draft_id, version_id, snapshot_hash),
        )
        self.connection.commit()
        issued = IssuedInvoiceService(self.ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        self.assertEqual(issued.tax_exclusive_amount, Decimal("0.003620"))
        self.assertEqual(issued.tax_amount, Decimal("0.000362"))
        self.assertEqual(issued.tax_inclusive_amount, Decimal("0.003982"))

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
