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
    CollectionCaseService,
    CollectionCaseSettlementService,
    CreditAdjustmentService,
    InvoiceDraftService,
    IssuedInvoiceService,
    PaymentIntentService,
    PostgresUsageLedger,
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetOverSignalPresentmentService,
    SpendBudgetOverSignalService,
    SpendBudgetPresentmentService,
    SpendBudgetService,
    TaxAssessmentService,
    TaxRateService,
    UsageIngestionService,
    WebhookDeliveryService,
    WebhookSubscriptionService,
    format_exact_decimal,
)
from metering_billing.accounting_export import AccountingExportService
from metering_billing.collection_write_off import CollectionWriteOffService
from metering_billing.errors import (
    RejectionReasonCode,
    SpendBudgetPresentmentQueryError,
    UsageEventConflict,
)
from metering_billing.payment_settlement import PaymentSettlementService
from metering_billing.rate_card import RateCardService
from metering_billing.spend_budget import compute_spend_budget_payload_hash
from metering_billing.time_window import TimeWindow
from metering_billing.usage_rating import UsageRatingService
from metering_billing.usage_ledger import StoredSpendBudget, StoredWebhookDeliveryAttempt
from metering_billing.webhook_outbox import (
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    EVENT_TYPE_SPEND_BUDGET_OVER,
    EVENT_TYPE_SPEND_BUDGET_PUBLISHED,
    enqueue_accepted_fact,
)
from tests.test_usage_rating import MORNING_WINDOW
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
        if len(applied) != 39:
            raise AssertionError(f"expected 39 migrations, got {len(applied)}")

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the dedicated integration connection."""
        cls.connection.close()

    def setUp(self) -> None:
        """Reset only the dedicated test database tables and seed two tenants."""
        self.connection.execute(
            """
            TRUNCATE TABLE
                billing_core.spend_budget,
                billing_core.journal_proposal_line,
                billing_core.journal_proposal,
                billing_core.collection_case_settlement,
                billing_core.collection_write_off,
                billing_core.webhook_delivery_attempt,
                billing_core.webhook_outbox_event,
                billing_core.webhook_subscription,
                billing_core.payment_receipt,
                billing_core.payment_intent,
                billing_core.credit_adjustment,
                billing_core.collection_dunning_event,
                billing_core.collection_case,
                billing_core.issued_invoice_line,
                billing_core.issued_invoice,
                billing_core.tax_assessment,
                billing_core.tax_rate_version,
                billing_core.tax_rate_schedule,
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
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            ({"metric_code": "gen_ai_output_token", "unit_amount": "2", "currency_code": "USD"},),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(datetime(2026, 8, 16, 10, tzinfo=UTC), datetime(2026, 8, 16, 12, tzinfo=UTC)),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        tenant_id = self.ledger.require_tenant(TENANT_ONE).tenant_account_id
        tax_rate = TaxRateService(self.ledger).publish_tax_rate(TENANT_ONE, "vat", "0.1")
        self.assertEqual(tax_rate.tax_rate_outcome_code.value, "accepted")
        tax_rate_replay = TaxRateService(self.ledger).publish_tax_rate(TENANT_ONE, "vat", "0.1")
        self.assertEqual(tax_rate_replay.tax_rate_outcome_code.value, "duplicate_replay")
        schedule = self.ledger.find_tax_rate_schedule(tenant_id, "vat")
        self.assertIsNotNone(schedule)
        assert schedule is not None
        self.assertEqual(self.ledger.insert_tax_rate_schedule(schedule), schedule)
        with self.assertRaises(ValueError):
            self.ledger.insert_tax_rate_schedule(replace(schedule, tax_code="gst"))
        self.assertEqual(self.ledger.list_tax_rate_schedules(tenant_id), (schedule,))
        self.assertEqual(self.ledger.get_tax_rate_schedule(schedule.tax_rate_schedule_id), schedule)
        version = self.ledger.get_tax_rate_version(tax_rate.tax_rate_version_id)
        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(
            self.ledger.find_tax_rate_version_by_identity(
                tenant_id,
                schedule.tax_rate_schedule_id,
                version.source_payload_hash,
                version.tax_rate_contract_version,
            ),
            version,
        )
        self.assertEqual(self.ledger.find_tax_rate_version(tenant_id, 1, "vat"), version)
        self.assertIsNone(self.ledger.find_tax_rate_version(tenant_id, 9))
        self.assertEqual(self.ledger.next_tax_rate_version_number(tenant_id, schedule.tax_rate_schedule_id), 2)
        self.assertEqual(self.ledger.insert_tax_rate_version(version), version)
        with self.assertRaises(ValueError):
            self.ledger.insert_tax_rate_version(
                replace(version, source_payload_hash="sha256:" + "2" * 64)
            )
        self.assertEqual(self.ledger.list_tax_rate_versions(tenant_id), (version,))
        self.assertEqual(
            self.ledger.list_tax_rate_versions(tenant_id, schedule.tax_rate_schedule_id),
            (version,),
        )
        assessment = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, draft.invoice_draft_id, version.tax_rate_version_id
        )
        self.assertEqual(assessment.tax_assessment_outcome_code.value, "accepted")
        assessment_replay = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, draft.invoice_draft_id, version.tax_rate_version_id
        )
        self.assertEqual(assessment_replay.tax_assessment_outcome_code.value, "duplicate_replay")
        stored_assessment = self.ledger.get_tax_assessment(assessment.tax_assessment_id)
        self.assertIsNotNone(stored_assessment)
        assert stored_assessment is not None
        self.assertEqual(self.ledger.insert_tax_assessment(stored_assessment), stored_assessment)
        with self.assertRaises(ValueError):
            self.ledger.insert_tax_assessment(
                replace(stored_assessment, source_payload_hash="sha256:" + "2" * 64)
            )
        self.assertEqual(self.ledger.find_tax_assessment_for_draft(tenant_id, draft.invoice_draft_id), stored_assessment)
        self.assertEqual(self.ledger.list_tax_assessments(), (stored_assessment,))
        self.assertEqual(self.ledger.list_tax_assessments(tenant_id), (stored_assessment,))
        self.assertEqual(
            self.ledger.find_tax_assessment(
                tenant_id,
                draft.invoice_draft_id,
                version.tax_rate_version_id,
                stored_assessment.source_payload_hash,
                stored_assessment.tax_assessment_contract_version,
            ),
            stored_assessment,
        )
        issued = IssuedInvoiceService(self.ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        self.assertEqual(issued.tax_exclusive_amount, Decimal("3620.000000000000"))
        self.assertEqual(issued.tax_amount, Decimal("362.00"))
        self.assertEqual(issued.tax_inclusive_amount, Decimal("3982.000000000000"))

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

    def test_collection_and_payment_intent_projection_are_durable(self) -> None:
        """Persist the next buyer-visible collection and payment-intent slice."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            ({"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},),
        )
        self.assertIsNotNone(card.rate_card_version_id)
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(
                datetime(2026, 8, 16, 10, tzinfo=UTC),
                datetime(2026, 8, 16, 12, tzinfo=UTC),
            ),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        case = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(case.collection_case_outcome_code.value, "accepted")
        assert case.collection_case_id is not None
        stored_case = self.ledger.get_collection_case(case.collection_case_id)
        self.assertIsNotNone(stored_case)
        assert stored_case is not None
        tenant_id = stored_case.tenant_account_id
        self.assertEqual(self.ledger.find_collection_case(tenant_id, draft.invoice_draft_id), stored_case)
        self.assertEqual(self.ledger.list_collection_cases(tenant_id), (stored_case,))
        self.assertEqual(self.ledger.insert_collection_case(
            replace(stored_case, collection_case_id=uuid4())
        ), stored_case)
        replay_case = CollectionCaseService(self.ledger).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(replay_case.collection_case_outcome_code.value, "duplicate_replay")
        self.assertIsNone(self.ledger.get_collection_case(uuid4()))
        self.assertIsNone(self.ledger.find_collection_case(tenant_id, uuid4()))

        dunning = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).record_dunning_event(
            TENANT_ONE, case.collection_case_id, "first_notice"
        )
        self.assertEqual(len(dunning.dunning_events), 1)
        stored_dunning = self.ledger.get_collection_dunning_event(dunning.dunning_events[0].dunning_event_id)
        self.assertIsNotNone(stored_dunning)
        assert stored_dunning is not None
        self.assertEqual(self.ledger.list_collection_dunning_events(case.collection_case_id), (stored_dunning,))
        self.assertEqual(self.ledger.list_collection_dunning_events_for_tenant(tenant_id), (stored_dunning,))
        self.assertEqual(
            self.ledger.find_collection_dunning_event(case.collection_case_id, "first_notice"),
            stored_dunning,
        )
        self.assertEqual(
            self.ledger.insert_collection_dunning_event(
                replace(stored_dunning, collection_dunning_event_id=uuid4())
            ),
            stored_dunning,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dunning_event(
                replace(stored_dunning, dunning_notice_code="invalid")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dunning_event(
                replace(stored_dunning, dunning_event_number=0, dunning_notice_code="overdue_notice")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dunning_event(
                replace(stored_dunning, collection_case_id=uuid4(), dunning_notice_code="overdue_notice")
            )
        self.assertIsNone(self.ledger.get_collection_dunning_event(uuid4()))

        intent = PaymentIntentService(self.ledger, clock=lambda: CATALOG_START).project_payment_intent(
            TENANT_ONE, case.collection_case_id
        )
        self.assertEqual(intent.payment_intent_outcome_code.value, "accepted")
        assert intent.payment_intent_id is not None
        stored_intent = self.ledger.get_payment_intent(intent.payment_intent_id)
        self.assertIsNotNone(stored_intent)
        assert stored_intent is not None
        self.assertEqual(
            self.ledger.find_payment_intent(
                tenant_id,
                case.collection_case_id,
                stored_intent.source_payload_hash,
                stored_intent.payment_intent_contract_version,
            ),
            stored_intent,
        )
        self.assertEqual(self.ledger.list_payment_intents(tenant_id), (stored_intent,))
        self.assertEqual(
            self.ledger.insert_payment_intent(
                replace(stored_intent, payment_intent_id=uuid4())
            ),
            stored_intent,
        )
        self.assertEqual(
            PaymentIntentService(self.ledger).project_payment_intent(
                TENANT_ONE, case.collection_case_id
            ).payment_intent_outcome_code.value,
            "duplicate_replay",
        )
        self.assertIsNone(self.ledger.get_payment_intent(uuid4()))
        self.assertIsNone(self.ledger.find_payment_intent(tenant_id, uuid4(), "sha256:" + "0" * 64, 1))
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_case(replace(stored_case, collection_case_status="settled"))
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_case(replace(stored_case, collection_case_id=uuid4(), outstanding_amount=Decimal("0")))
        with self.assertRaises(ValueError):
            self.ledger.insert_payment_intent(replace(stored_intent, payment_intent_status="captured"))
        with self.assertRaises(ValueError):
            self.ledger.insert_payment_intent(replace(stored_intent, payment_intent_id=uuid4(), payment_amount=Decimal("0")))
        cancelled = self.ledger.cancel_stored_payment_intent(intent.payment_intent_id)
        self.assertEqual(cancelled.payment_intent_status, "cancelled")
        self.assertEqual(self.ledger.cancel_stored_payment_intent(intent.payment_intent_id), cancelled)
        with self.assertRaises(ValueError):
            self.ledger.cancel_stored_payment_intent(uuid4())
        rejected_intent = self.ledger.insert_payment_intent(
            replace(
                stored_intent,
                payment_intent_id=uuid4(),
                payment_intent_status="rejected",
                source_payload_hash="sha256:" + "1" * 64,
            )
        )
        with self.assertRaises(ValueError):
            self.ledger.cancel_stored_payment_intent(rejected_intent.payment_intent_id)
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_settlement(uuid4(), Decimal("1"))
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'disputed' WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_settlement(case.collection_case_id, Decimal("1"))
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'voided', outstanding_amount = 0 WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_settlement(case.collection_case_id, Decimal("1"))
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'open', outstanding_amount = %s WHERE collection_case_id = %s",
            (stored_case.outstanding_amount, case.collection_case_id),
        )
        self.connection.commit()
        settled = self.ledger.apply_collection_settlement(
            case.collection_case_id, stored_case.outstanding_amount
        )
        self.assertEqual(settled.collection_case_status, "settled")
        self.assertEqual(settled.outstanding_amount, Decimal("0.000000000000"))
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_settlement(case.collection_case_id, Decimal("1"))
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_settlement(case.collection_case_id, Decimal("0"))

    def test_payment_receipt_and_cash_journal_are_durable(self) -> None:
        """Persist a receipt, atomic collection settlement, and cash proposal."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        self.assertIsNotNone(card.rate_card_version_id)
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(
                datetime(2026, 8, 16, 10, tzinfo=UTC),
                datetime(2026, 8, 16, 12, tzinfo=UTC),
            ),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        case = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        assert case.collection_case_id is not None
        intent = PaymentIntentService(self.ledger, clock=lambda: CATALOG_START).project_payment_intent(
            TENANT_ONE, case.collection_case_id
        )
        assert intent.payment_intent_id is not None
        amount = self.ledger.get_collection_case(case.collection_case_id).outstanding_amount / Decimal("2")
        settlement = PaymentSettlementService(self.ledger, clock=lambda: CATALOG_START)
        accepted = settlement.record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, amount
        )
        self.assertEqual(accepted.payment_settlement_outcome_code.value, "accepted")
        assert accepted.payment_receipt_id is not None
        receipt = self.ledger.get_payment_receipt(accepted.payment_receipt_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(self.ledger.list_payment_receipts(receipt.tenant_account_id), (receipt,))
        self.assertEqual(
            self.ledger.find_payment_receipt(
                receipt.tenant_account_id,
                receipt.payment_intent_id,
                receipt.source_payload_hash,
                receipt.settlement_contract_version,
            ),
            receipt,
        )
        settled_case = self.ledger.get_collection_case(receipt.collection_case_id)
        self.assertIsNotNone(settled_case)
        assert settled_case is not None
        self.assertEqual(settled_case.collection_case_status, "open")
        self.assertEqual(settled_case.outstanding_amount, receipt.received_amount)

        proposals = self.ledger.list_journal_proposals(receipt.tenant_account_id)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(len(proposal.proposal_lines), 2)
        self.assertEqual(proposal.proposal_lines[0].debit_amount, receipt.received_amount)
        self.assertEqual(proposal.proposal_lines[1].credit_amount, receipt.received_amount)
        self.assertEqual(self.ledger.get_journal_proposal(proposal.journal_proposal_id), proposal)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_receipt(
                receipt.tenant_account_id,
                receipt.payment_receipt_id,
                proposal.source_payload_hash,
                proposal.proposal_contract_version,
            ),
            proposal,
        )
        self.assertEqual(
            AccountingExportService(self.ledger).propose_cash_journal(
                TENANT_ONE, receipt.payment_receipt_id
            ).journal_proposal_outcome_code.value,
            "duplicate_replay",
        )
        replay = settlement.record_payment_receipt(TENANT_ONE, intent.payment_intent_id, amount)
        self.assertEqual(replay.payment_settlement_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(self.ledger.list_payment_receipts(receipt.tenant_account_id), (receipt,))

        class ExistingReceiptLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_payment_receipt(self, *args, **kwargs):
                return None

        race_replay = PaymentSettlementService(
            ExistingReceiptLedger(self.connection), clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, amount)
        self.assertEqual(race_replay.payment_settlement_outcome_code.value, "duplicate_replay")
        self.assertEqual(race_replay.payment_receipt_id, receipt.payment_receipt_id)

        class MissingCaseReceiptLedger(ExistingReceiptLedger):
            """Exercise the fail-closed branch after a concurrent receipt replay."""

            def __init__(self, connection):
                super().__init__(connection)
                self._case_reads = 0

            def get_collection_case(self, collection_case_id):
                self._case_reads += 1
                if self._case_reads == 2:
                    return None
                return super().get_collection_case(collection_case_id)

        missing_case_replay = PaymentSettlementService(
            MissingCaseReceiptLedger(self.connection), clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, amount)
        self.assertEqual(missing_case_replay.payment_settlement_outcome_code.value, "rejected")

        self.assertEqual(
            self.ledger.insert_journal_proposal(proposal, proposal.proposal_lines), proposal
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_payment_receipt(
                replace(receipt, source_payload_hash="sha256:" + "2" * 64)
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(proposal, source_payload_hash="sha256:" + "3" * 64),
                proposal.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(proposal, proposal_status="posted"), proposal.proposal_lines
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(proposal, ())
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (replace(proposal.proposal_lines[0], journal_proposal_id=uuid4()),
                 proposal.proposal_lines[1]),
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (replace(proposal.proposal_lines[0], tenant_account_id=uuid4()),
                 proposal.proposal_lines[1]),
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (replace(proposal.proposal_lines[0], credit_amount=Decimal("1")),
                 proposal.proposal_lines[1]),
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (replace(proposal.proposal_lines[0], debit_amount=Decimal("1")),
                 proposal.proposal_lines[1]),
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (
                    replace(proposal.proposal_lines[0], debit_amount=Decimal("0.0000001")),
                    replace(proposal.proposal_lines[1], credit_amount=Decimal("0.0000001")),
                ),
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                proposal,
                (
                    replace(proposal.proposal_lines[0], debit_amount=Decimal("10")),
                    replace(proposal.proposal_lines[1], credit_amount=Decimal("10.0000001")),
                ),
            )

        with self.assertRaises(ValueError):
            self.ledger.insert_payment_receipt(replace(receipt, payment_receipt_status="captured"))
        with self.assertRaises(ValueError):
            self.ledger.insert_payment_receipt(replace(receipt, received_amount=Decimal("0")))
        self.assertIsNone(self.ledger.get_payment_receipt(uuid4()))
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))

    def test_credit_adjustment_and_credit_journal_are_durable(self) -> None:
        """Persist a credit, its tax split, collection reduction, and journal."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        self.assertIsNotNone(card.rate_card_version_id)
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(
                datetime(2026, 8, 16, 10, tzinfo=UTC),
                datetime(2026, 8, 16, 12, tzinfo=UTC),
            ),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        case = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        assert case.collection_case_id is not None
        stored_case = self.ledger.get_collection_case(case.collection_case_id)
        assert stored_case is not None
        amount = stored_case.outstanding_amount / Decimal("2")
        credit = CreditAdjustmentService(self.ledger, clock=lambda: CATALOG_START).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        assert credit.proposal_id is not None
        stored = self.ledger.get_credit_adjustment(credit.credit_adjustment_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.credit_amount, amount)
        self.assertEqual(stored.tax_exclusive_amount, amount)
        self.assertEqual(stored.tax_amount, Decimal("0"))
        self.assertEqual(self.ledger.list_credit_adjustments(stored.tenant_account_id), (stored,))
        self.assertEqual(
            self.ledger.find_credit_adjustment(
                stored.tenant_account_id,
                stored.invoice_draft_id,
                stored.source_payload_hash,
                stored.credit_adjustment_contract_version,
            ),
            stored,
        )
        self.assertIsNone(self.ledger.get_credit_adjustment(uuid4()))
        proposal = self.ledger.get_journal_proposal(credit.proposal_id)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.credit_adjustment_id, stored.credit_adjustment_id)
        self.assertEqual(len(proposal.proposal_lines), 2)
        self.assertEqual(proposal.proposal_lines[0].debit_amount, amount)
        self.assertEqual(proposal.proposal_lines[1].credit_amount, amount)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_credit(
                stored.tenant_account_id,
                stored.credit_adjustment_id,
                proposal.source_payload_hash,
                proposal.proposal_contract_version,
            ),
            proposal,
        )
        remaining_case = self.ledger.get_collection_case(stored_case.collection_case_id)
        self.assertIsNotNone(remaining_case)
        assert remaining_case is not None
        self.assertEqual(remaining_case.outstanding_amount, amount)
        replay = CreditAdjustmentService(self.ledger).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, amount, "goodwill"
        )
        self.assertEqual(replay.credit_adjustment_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.credit_adjustment_id, stored.credit_adjustment_id)
        self.assertEqual(
            self.ledger.insert_journal_proposal(proposal, proposal.proposal_lines), proposal
        )

        class ExistingCreditLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent credit race."""

            def find_credit_adjustment(self, *args, **kwargs):
                return None

        race_replay = CreditAdjustmentService(
            ExistingCreditLedger(self.connection), clock=lambda: CATALOG_START
        ).record_credit_adjustment(TENANT_ONE, draft.invoice_draft_id, amount, "goodwill")
        self.assertEqual(race_replay.credit_adjustment_outcome_code.value, "duplicate_replay")
        self.assertEqual(race_replay.credit_adjustment_id, stored.credit_adjustment_id)

        class MissingCreditProposalLedger(ExistingCreditLedger):
            """Exercise the fail-closed branch when a raced journal is absent."""

            def find_journal_proposal_for_credit(self, *args, **kwargs):
                return None

        missing_proposal = CreditAdjustmentService(
            MissingCreditProposalLedger(self.connection), clock=lambda: CATALOG_START
        ).record_credit_adjustment(TENANT_ONE, draft.invoice_draft_id, amount, "goodwill")
        self.assertEqual(missing_proposal.credit_adjustment_outcome_code.value, "rejected")

        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(proposal, payment_receipt_id=None, credit_adjustment_id=None),
                proposal.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_adjustment(
                replace(stored, source_payload_hash="sha256:" + "4" * 64)
            )
        self.assertEqual(
            self.ledger.insert_credit_adjustment(replace(stored, credit_adjustment_id=uuid4())),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_adjustment(
                replace(stored, credit_reason_code="invalid")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_adjustment(
                replace(stored, credit_amount=Decimal("0"), tax_exclusive_amount=Decimal("0"))
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_adjustment(
                replace(stored, tax_exclusive_amount=Decimal("1"))
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_adjustment(
                replace(stored, tax_exclusive_amount=Decimal("-1"))
            )

    def test_collection_write_off_and_settlement_are_durable(self) -> None:
        """Persist a zeroing write-off and the explicit settlement fact."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        card = RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        self.assertIsNotNone(card.rate_card_version_id)
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE,
            TimeWindow(
                datetime(2026, 8, 16, 10, tzinfo=UTC),
                datetime(2026, 8, 16, 12, tzinfo=UTC),
            ),
            1,
            rate_card_code="cwl_standard",
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        assert opened.collection_case_id is not None
        case = self.ledger.get_collection_case(opened.collection_case_id)
        assert case is not None
        write_off = CollectionWriteOffService(self.ledger, clock=lambda: CATALOG_START).write_off_collection_case(
            TENANT_ONE, case.collection_case_id
        )
        self.assertEqual(write_off.collection_write_off_outcome_code.value, "accepted")
        assert write_off.collection_write_off_id is not None
        stored_write_off = self.ledger.get_collection_write_off(write_off.collection_write_off_id)
        self.assertIsNotNone(stored_write_off)
        assert stored_write_off is not None
        self.assertEqual(
            self.ledger.find_collection_write_off(case.tenant_account_id, case.collection_case_id),
            stored_write_off,
        )
        self.assertEqual(
            self.ledger.list_collection_write_offs_for_tenant(case.tenant_account_id),
            (stored_write_off,),
        )
        self.assertEqual(
            self.ledger.insert_collection_write_off(stored_write_off), stored_write_off
        )
        tenant_two_id = self.ledger.require_tenant(TENANT_TWO).tenant_account_id
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_write_off(
                replace(stored_write_off, tenant_account_id=tenant_two_id)
            )
        zero_case = self.ledger.get_collection_case(case.collection_case_id)
        self.assertIsNotNone(zero_case)
        assert zero_case is not None
        self.assertEqual(zero_case.collection_case_status, "open")
        self.assertEqual(zero_case.outstanding_amount, Decimal("0.000000000000"))
        settled = CollectionCaseSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).settle_collection_case(TENANT_ONE, case.collection_case_id)
        self.assertEqual(settled.collection_case_settlement_outcome_code.value, "accepted")
        assert settled.collection_case_settlement_id is not None
        stored_settlement = self.ledger.get_collection_case_settlement(
            settled.collection_case_settlement_id
        )
        self.assertIsNotNone(stored_settlement)
        assert stored_settlement is not None
        self.assertEqual(
            self.ledger.find_collection_case_settlement(
                case.tenant_account_id, case.collection_case_id
            ),
            stored_settlement,
        )
        self.assertEqual(
            self.ledger.list_collection_case_settlements_for_tenant(case.tenant_account_id),
            (stored_settlement,),
        )
        self.assertEqual(
            CollectionWriteOffService(self.ledger).write_off_collection_case(
                TENANT_ONE, case.collection_case_id
            ).collection_write_off_outcome_code.value,
            "duplicate_replay",
        )
        self.assertEqual(
            CollectionCaseSettlementService(self.ledger).settle_collection_case(
                TENANT_ONE, case.collection_case_id
            ).collection_case_settlement_outcome_code.value,
            "duplicate_replay",
        )
        self.assertEqual(
            self.ledger.insert_collection_case_settlement(stored_settlement), stored_settlement
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_case_settlement(
                replace(stored_settlement, tenant_account_id=tenant_two_id)
            )
        self.assertIsNone(self.ledger.get_collection_write_off(uuid4()))
        self.assertIsNone(self.ledger.get_collection_case_settlement(uuid4()))
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_write_off(
                replace(stored_write_off, remaining_outstanding_amount=Decimal("1"))
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_write_off(
                replace(stored_write_off, collection_write_off_status="invalid")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_write_off(
                replace(stored_write_off, write_off_amount=Decimal("0"))
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_case_settlement(
                replace(stored_settlement, collection_case_settlement_status="invalid")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_case_settlement(
                replace(stored_settlement, remaining_outstanding_amount=Decimal("1"))
            )
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_write_off(case.collection_case_id, Decimal("0"))
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_write_off(uuid4(), Decimal("1"))
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_settled(uuid4())
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_write_off(case.collection_case_id, Decimal("1"))
        self.assertEqual(
            self.ledger.mark_collection_case_settled(case.collection_case_id),
            self.ledger.get_collection_case(case.collection_case_id),
        )
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'open' "
            "WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.apply_collection_write_off(case.collection_case_id, Decimal("1"))
        self.connection.execute(
            "UPDATE billing_core.collection_case SET outstanding_amount = 1 "
            "WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_settled(case.collection_case_id)
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'voided', "
            "outstanding_amount = 0 WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_settled(case.collection_case_id)
        self.connection.execute(
            "UPDATE billing_core.collection_case SET collection_case_status = 'disputed' "
            "WHERE collection_case_id = %s",
            (case.collection_case_id,),
        )
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_settled(case.collection_case_id)

    def test_webhook_subscription_outbox_and_delivery_are_durable(self) -> None:
        """Persist subscription metadata, attempts, and delivery status in PostgreSQL."""
        subscription_service = WebhookSubscriptionService(
            self.ledger, clock=lambda: CATALOG_START
        )
        registered = subscription_service.register_subscription(
            TENANT_ONE,
            "https://hooks.example.test/cwl",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,),
        )
        self.assertEqual(registered.webhook_subscription_outcome_code.value, "accepted")
        self.assertIsNotNone(registered.webhook_secret)
        replay = subscription_service.register_subscription(
            TENANT_ONE,
            "https://hooks.example.test/cwl",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,),
        )
        self.assertEqual(replay.webhook_subscription_outcome_code.value, "duplicate_replay")
        self.assertIsNone(replay.webhook_secret)
        assert registered.webhook_subscription_id is not None
        stored = self.ledger.get_webhook_subscription(registered.webhook_subscription_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.webhook_secret_hash.startswith("hmac-sha256:"))
        self.assertEqual(
            self.ledger.list_active_webhook_subscriptions(
                stored.tenant_account_id, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ),
            (stored,),
        )
        self.assertEqual(self.ledger.list_active_webhook_subscriptions(
            self.ledger.register_tenant(TENANT_TWO).tenant_account_id,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
        ), ())

        source_id = uuid4()
        outbox = enqueue_accepted_fact(
            self.ledger,
            TENANT_ONE,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            source_id,
            {"proposal_id": str(source_id)},
            CATALOG_START,
        )
        self.assertIsNotNone(outbox)
        assert outbox is not None
        self.assertEqual(self.ledger.get_webhook_outbox_event(outbox.outbox_event_id), outbox)
        self.assertEqual(self.ledger.find_webhook_outbox_event(
            stored.tenant_account_id,
            outbox.event_type_code,
            source_id,
            outbox.payload_hash,
        ), outbox)
        self.assertEqual(
            enqueue_accepted_fact(
                self.ledger,
                TENANT_ONE,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                source_id,
                {"proposal_id": str(source_id)},
                CATALOG_START,
            ),
            outbox,
        )
        delivered = WebhookDeliveryService(
            self.ledger,
            clock=lambda: CATALOG_START,
            transport=lambda url, body, headers: (200, None),
        ).deliver_due_events(TENANT_ONE)
        self.assertEqual(delivered.delivered_event_count, 1)
        self.assertEqual(delivered.attempted_delivery_count, 1)
        self.assertEqual(self.ledger.list_pending_webhook_outbox_events(
            stored.tenant_account_id
        ), ())
        delivered_row = self.ledger.mark_webhook_outbox_event_delivered(outbox.outbox_event_id)
        self.assertEqual(delivered_row.delivery_status, "delivered")
        attempts = self.ledger.list_webhook_delivery_attempts(
            outbox.outbox_event_id, registered.webhook_subscription_id
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].attempt_number, 1)
        self.assertEqual(self.ledger.get_webhook_delivery_attempt(attempts[0].delivery_attempt_id), attempts[0])
        self.assertEqual(
            self.ledger.list_webhook_delivery_attempts_for_tenant(stored.tenant_account_id),
            attempts,
        )
        self.assertIsNone(self.ledger.get_webhook_subscription(uuid4()))
        self.assertIsNone(self.ledger.find_webhook_subscription(
            stored.tenant_account_id,
            "https://hooks.example.test/missing",
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            1,
        ))
        self.assertIsNone(self.ledger.get_webhook_outbox_event(uuid4()))
        self.assertIsNone(self.ledger.get_webhook_delivery_attempt(uuid4()))

        revoked = subscription_service.revoke_subscription(
            TENANT_ONE, registered.webhook_subscription_id
        )
        self.assertEqual(revoked.subscription_status, "revoked")
        self.assertEqual(
            subscription_service.revoke_subscription(
                TENANT_ONE, registered.webhook_subscription_id
            ).webhook_subscription_outcome_code.value,
            "duplicate_replay",
        )
        self.assertEqual(
            self.ledger.list_webhook_subscriptions(stored.tenant_account_id),
            (self.ledger.get_webhook_subscription(registered.webhook_subscription_id),),
        )
        self.assertEqual(
            self.ledger.revoke_webhook_subscription(
                registered.webhook_subscription_id, CATALOG_START
            ).subscription_status,
            "revoked",
        )
        with self.assertRaises(ValueError):
            self.ledger.revoke_webhook_subscription(uuid4(), CATALOG_START)
        with self.assertRaises(ValueError):
            self.ledger.mark_webhook_outbox_event_delivered(uuid4())
        with self.assertRaises(ValueError):
            self.ledger.store_webhook_subscription_secret(registered.webhook_subscription_id, "")
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_subscription(
                replace(stored, subscription_status="invalid", webhook_subscription_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_subscription(
                replace(stored, webhook_secret_hash="invalid", webhook_subscription_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_subscription(
                replace(
                    stored,
                    webhook_subscription_id=uuid4(),
                    callback_url="https://hooks.example.test/conflict",
                )
            )
        replay_by_database = self.ledger.insert_webhook_subscription(
            replace(stored, webhook_subscription_id=uuid4())
        )
        self.assertEqual(
            replay_by_database,
            self.ledger.get_webhook_subscription(registered.webhook_subscription_id),
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_delivery_attempt(
                replace(attempts[0], delivery_attempt_id=uuid4(), attempt_number=0)
            )
        self.assertEqual(
            self.ledger.insert_webhook_delivery_attempt(attempts[0]), attempts[0]
        )
        self.assertEqual(
            self.ledger.list_webhook_delivery_attempts(outbox.outbox_event_id), attempts
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_webhook_delivery_attempt(
                StoredWebhookDeliveryAttempt(
                    uuid4(), uuid4(), registered.webhook_subscription_id, 1,
                    500, None, "missing", CATALOG_START
                )
            )

        class ReplayOnInsertLedger(PostgresUsageLedger):
            def find_webhook_subscription(self, *args, **kwargs):
                return None

            def insert_webhook_subscription(self, candidate):
                stored_candidate = super().find_webhook_subscription(
                    candidate.tenant_account_id,
                    candidate.callback_url,
                    candidate.event_type_set,
                    candidate.webhook_subscription_contract_version,
                )
                assert stored_candidate is not None
                return stored_candidate

        concurrent_replay = WebhookSubscriptionService(
            ReplayOnInsertLedger(self.connection), clock=lambda: CATALOG_START
        ).register_subscription(
            TENANT_ONE,
            "https://hooks.example.test/cwl",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,),
        )
        self.assertEqual(concurrent_replay.webhook_subscription_outcome_code.value, "duplicate_replay")

    def test_published_spend_budget_is_durable(self) -> None:
        """Persist one published spend_budget and keep existing reads after restart."""
        tenant = self.ledger.require_tenant(TENANT_ONE)
        account, account_error = self.ledger.resolve_billing_account(tenant, ACCOUNT_ONE)
        self.assertIsNone(account_error)
        assert account is not None
        self.assertEqual(self.ledger.get_billing_account(account.billing_account_id), account)
        self.assertIsNone(self.ledger.get_billing_account(uuid4()))
        published_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        budget_amount = Decimal("100.00")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        accepted = SpendBudgetService(
            self.ledger, clock=lambda: published_at
        ).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            budget_amount,
            MORNING_WINDOW,
        )
        self.assertEqual(accepted.spend_budget_outcome_code.value, "accepted")
        assert accepted.spend_budget_id is not None
        stored = self.ledger.get_spend_budget(accepted.spend_budget_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.billing_account_id, account.billing_account_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.budget_amount, budget_amount)
        self.assertEqual(stored.window_started_at, MORNING_WINDOW.window_started_at)
        self.assertEqual(stored.window_ended_at, MORNING_WINDOW.window_ended_at)
        self.assertEqual(stored.published_at, published_at)
        self.assertEqual(stored.spend_budget_status, "published")
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertEqual(stored.spend_budget_contract_version, 1)
        self.assertIsInstance(stored.budget_amount, Decimal)
        self.assertNotIsInstance(stored.budget_amount, float)
        self.assertEqual(
            self.ledger.find_spend_budget(
                stored.tenant_account_id,
                stored.billing_account_id,
                stored.window_started_at,
                stored.window_ended_at,
                stored.currency_code,
                stored.source_payload_hash,
                stored.spend_budget_contract_version,
            ),
            stored,
        )
        self.assertEqual(self.ledger.list_spend_budgets(stored.tenant_account_id), (stored,))
        self.assertEqual(
            self.ledger.list_spend_budgets(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_spend_budget(uuid4()))
        self.assertIsNone(
            self.ledger.find_spend_budget(
                stored.tenant_account_id,
                stored.billing_account_id,
                stored.window_started_at,
                stored.window_ended_at,
                "EUR",
                stored.source_payload_hash,
                stored.spend_budget_contract_version,
            )
        )
        row_count = self.connection.execute(
            "SELECT COUNT(*) FROM billing_core.spend_budget"
        ).fetchone()[0]
        self.assertEqual(row_count, 1)
        status_code = self.connection.execute(
            "SELECT spend_budget_status FROM billing_core.spend_budget WHERE spend_budget_id = %s",
            (stored.spend_budget_id,),
        ).fetchone()[0]
        self.assertEqual(status_code, "published")
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.spend_budget_id)

        replay = SpendBudgetService(self.ledger, clock=lambda: published_at).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            budget_amount,
            MORNING_WINDOW,
        )
        self.assertEqual(replay.spend_budget_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.spend_budget_id, stored.spend_budget_id)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM billing_core.spend_budget").fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
                ]
            ),
            1,
        )

        rejected = SpendBudgetService(self.ledger).publish_spend_budget(
            TENANT_ONE, uuid4(), "USD", budget_amount, MORNING_WINDOW
        )
        self.assertEqual(rejected.spend_budget_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM billing_core.spend_budget").fetchone()[0],
            1,
        )
        mismatch = SpendBudgetService(self.ledger).publish_spend_budget(
            TENANT_TWO,
            account.billing_account_id,
            "USD",
            budget_amount,
            MORNING_WINDOW,
        )
        self.assertEqual(mismatch.spend_budget_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM billing_core.spend_budget").fetchone()[0],
            1,
        )

        later = SpendBudgetService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            Decimal("250.00"),
            MORNING_WINDOW,
        )
        self.assertEqual(later.spend_budget_outcome_code.value, "accepted")
        self.assertNotEqual(later.spend_budget_id, stored.spend_budget_id)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM billing_core.spend_budget").fetchone()[0],
            2,
        )

        crash_payload_hash = compute_spend_budget_payload_hash(
            {
                "billing_account_id": str(account.billing_account_id),
                "currency_code": "KRW",
                "budget_amount": format_exact_decimal(Decimal("1000")),
                "window_started_at": "2026-08-16T10:00:00Z",
                "window_ended_at": "2026-08-16T11:00:00Z",
                "spend_budget_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_spend_budget(
            StoredSpendBudget(
                spend_budget_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                billing_account_id=account.billing_account_id,
                spend_budget_contract_version=1,
                currency_code="KRW",
                budget_amount=Decimal("1000"),
                window_started_at=MORNING_WINDOW.window_started_at,
                window_ended_at=MORNING_WINDOW.window_ended_at,
                source_payload_hash=crash_payload_hash,
                published_at=datetime(2026, 8, 18, 17, 0, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
            ]
        )
        healed = SpendBudgetService(
            self.ledger, clock=lambda: published_at
        ).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "KRW",
            Decimal("1000"),
            MORNING_WINDOW,
        )
        self.assertEqual(healed.spend_budget_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.spend_budget_id, inserted_without_outbox.spend_budget_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_PUBLISHED
                ]
            ),
            prior_outbox + 1,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_spend_budget(stored.spend_budget_id)
        self.assertEqual(reloaded, stored)
        presentment = SpendBudgetPresentmentService(fresh).present_spend_budget(
            TENANT_ONE, stored.spend_budget_id
        )
        self.assertEqual(presentment.budget_amount, budget_amount)
        self.assertEqual(presentment.spend_budget_status, "published")
        self.assertEqual(presentment.next_operator_action, "wait")
        evaluation = SpendBudgetEvaluationPresentmentService(fresh).present_spend_budget_evaluation(
            TENANT_ONE, stored.spend_budget_id
        )
        self.assertEqual(evaluation.budget_amount, budget_amount)
        self.assertEqual(evaluation.utilization_status, "under")
        self.assertEqual(evaluation.spend_budget_status, "published")
        statuses = SpendBudgetEvaluationPresentmentService(fresh).list_billing_account_budget_statuses(
            TENANT_ONE, account.billing_account_id
        )
        self.assertGreaterEqual(len(statuses.budget_statuses), 1)
        self.assertEqual(
            {row.spend_budget_id for row in statuses.budget_statuses},
            {
                stored.spend_budget_id,
                later.spend_budget_id,
                inserted_without_outbox.spend_budget_id,
            },
        )
        self.assertEqual(statuses.budget_statuses[0].spend_budget_id, stored.spend_budget_id)
        over_signal = SpendBudgetOverSignalService(fresh, clock=lambda: published_at).observe_spend_budget_over(
            TENANT_ONE, stored.spend_budget_id
        )
        self.assertEqual(over_signal.spend_budget_over_signal_outcome_code.value, "accepted")
        self.assertEqual(over_signal.utilization_status, "under")
        self.assertEqual(
            len(
                [
                    event
                    for event in fresh.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER
                ]
            ),
            0,
        )
        under_observation = SpendBudgetOverSignalPresentmentService(
            fresh
        ).present_spend_budget_over_signal(TENANT_ONE, stored.spend_budget_id)
        self.assertEqual(under_observation.over_signal.utilization_status, "under")
        self.assertEqual(under_observation.webhook_outbox_events, ())
        over_budget = SpendBudgetService(fresh, clock=lambda: published_at).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            Decimal("0.001"),
            MORNING_WINDOW,
        )
        self.assertEqual(over_budget.spend_budget_outcome_code.value, "accepted")
        assert over_budget.spend_budget_id is not None
        first_over = SpendBudgetOverSignalService(fresh, clock=lambda: published_at).observe_spend_budget_over(
            TENANT_ONE, over_budget.spend_budget_id
        )
        self.assertEqual(first_over.spend_budget_over_signal_outcome_code.value, "accepted")
        self.assertEqual(first_over.utilization_status, "over")
        over_events = [
            event
            for event in fresh.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)
            if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER
        ]
        self.assertEqual(len(over_events), 1)
        self.assertEqual(over_events[0].source_id, over_budget.spend_budget_id)
        replay_over = SpendBudgetOverSignalService(fresh, clock=lambda: published_at).observe_spend_budget_over(
            TENANT_ONE, over_budget.spend_budget_id
        )
        self.assertEqual(replay_over.spend_budget_over_signal_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            len(
                [
                    event
                    for event in fresh.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_OVER
                ]
            ),
            1,
        )
        over_observation = SpendBudgetOverSignalPresentmentService(
            fresh
        ).present_spend_budget_over_signal(TENANT_ONE, over_budget.spend_budget_id)
        self.assertEqual(over_observation.over_signal.utilization_status, "over")
        self.assertEqual(len(over_observation.webhook_outbox_events), 1)
        self.assertEqual(
            over_observation.webhook_outbox_events[0].event_type_code,
            EVENT_TYPE_SPEND_BUDGET_OVER,
        )
        self.assertEqual(
            over_observation.webhook_outbox_events[0].source_id,
            over_budget.spend_budget_id,
        )
        reloaded_observation = SpendBudgetOverSignalPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_spend_budget_over_signal(TENANT_ONE, over_budget.spend_budget_id)
        self.assertEqual(len(reloaded_observation.webhook_outbox_events), 1)
        self.assertEqual(
            reloaded_observation.webhook_outbox_events[0].outbox_event_id,
            over_observation.webhook_outbox_events[0].outbox_event_id,
        )
        # HTTP create_http_app still requires #22 credential methods on the ledger.
        with self.assertRaises(SpendBudgetPresentmentQueryError) as missing_pin:
            SpendBudgetPresentmentService(fresh).present_spend_budget(
                "", stored.spend_budget_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(SpendBudgetPresentmentQueryError) as other_pin:
            SpendBudgetPresentmentService(fresh).present_spend_budget(
                TENANT_TWO, stored.spend_budget_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "spend_budget_not_found")

        self.assertEqual(self.ledger.insert_spend_budget(stored), stored)
        self.assertEqual(
            self.ledger.insert_spend_budget(replace(stored, spend_budget_id=uuid4())),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_spend_budget(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_spend_budget(replace(stored, source_payload_hash="md5:abc"))
        with self.assertRaises(ValueError):
            self.ledger.insert_spend_budget(
                replace(stored, budget_amount=Decimal("0"), spend_budget_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_spend_budget(replace(stored, spend_budget_status="posted"))
        with self.assertRaises(ValueError):
            self.ledger.insert_spend_budget(
                replace(
                    stored,
                    spend_budget_id=later.spend_budget_id,
                    source_payload_hash="sha256:" + "d" * 64,
                    currency_code="EUR",
                    budget_amount=Decimal("5"),
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_spend_budget(self, *args, **kwargs):
                return None

        raced = SpendBudgetService(
            BlindFindLedger(self.connection), clock=lambda: published_at
        ).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            budget_amount,
            MORNING_WINDOW,
        )
        self.assertEqual(raced.spend_budget_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.spend_budget_id, stored.spend_budget_id)

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
