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
    CollectionDisputePresentmentService,
    CollectionDisputeReleasePresentmentService,
    CollectionDisputeReleaseService,
    CollectionDisputeService,
    CreditAdjustmentService,
    CreditNoteApplicationPresentmentService,
    CreditNoteApplicationService,
    InvoiceDraftService,
    IssuedCreditNotePresentmentService,
    IssuedCreditNoteService,
    IssuedCreditNoteVoidPresentmentService,
    IssuedCreditNoteVoidService,
    IssuedInvoiceService,
    IssuedInvoiceVoidPresentmentService,
    IssuedInvoiceVoidService,
    PaymentIntentService,
    PostgresUsageLedger,
    UnappliedCashApplicationPresentmentService,
    UnappliedCashApplicationService,
    UnappliedCashPresentmentService,
    UnappliedCashRefundPresentmentService,
    UnappliedCashRefundService,
    UnappliedCashService,
    SpendBudgetEvaluationPresentmentService,
    SpendBudgetApproachingSignalPresentmentService,
    SpendBudgetApproachingSignalService,
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
    validate_reconciliation_evidence,
    validate_reconciliation_run,
    validate_reconciliation_resolution,
)
from metering_billing.accounting_export import AccountingExportService
from metering_billing.collection_dispute import compute_dispute_payload_hash
from metering_billing.collection_write_off import CollectionWriteOffService
from metering_billing.credit_note_application import compute_application_payload_hash
from metering_billing.unapplied_cash_application import (
    compute_unapplied_cash_application_payload_hash,
)
from metering_billing.unapplied_cash_refund import (
    compute_unapplied_cash_refund_payload_hash,
)
from metering_billing.errors import (
    CollectionDisputePresentmentQueryError,
    CollectionDisputeRejectionReasonCode,
    CollectionDisputeReleasePresentmentQueryError,
    CollectionWriteOffRejectionReasonCode,
    CreditNoteApplicationPresentmentQueryError,
    IssuedCreditNotePresentmentQueryError,
    IssuedCreditNoteVoidPresentmentQueryError,
    IssuedInvoiceVoidPresentmentQueryError,
    JournalProposalQueryError,
    JournalProposalRejectionReasonCode,
    RejectionReasonCode,
    SpendBudgetPresentmentQueryError,
    IssuedInvoiceVoidRejectionReasonCode,
    UnappliedCashApplicationPresentmentQueryError,
    UnappliedCashApplicationRejectionReasonCode,
    UnappliedCashPresentmentQueryError,
    UnappliedCashRefundPresentmentQueryError,
    UsageEventConflict,
)
from metering_billing.issued_credit_note import compute_issued_credit_note_payload_hash
from metering_billing.issued_credit_note_void import (
    compute_issued_credit_note_void_payload_hash,
)
from metering_billing.issued_invoice_void import (
    compute_issued_invoice_void_payload_hash,
)
from metering_billing.payment_settlement import PaymentSettlementService
from metering_billing.period_close import (
    _NEXT_ACTIONS,
    ReconciliationException,
    ReconciliationExceptionCode,
    ReconciliationEvidence,
    ReconciliationLine,
    ReconciliationLineStatus,
    ReconciliationRun,
    ReconciliationResolution,
    ReconciliationResolutionStatus,
    assess_reconciliation_line,
    convert_currency_amount,
    create_billing_period,
    create_fx_rate,
)
from metering_billing.rate_card import RateCardService
from metering_billing.spend_budget import compute_spend_budget_payload_hash
from metering_billing.tenant_api_credential import (
    TenantApiCredentialQueryError,
    TenantApiCredentialService,
    hash_api_credential_secret,
)
from metering_billing.time_window import TimeWindow
from metering_billing.usage_rating import UsageRatingService
from metering_billing.usage_ledger import (
    StoredCollectionDispute,
    StoredCreditNoteApplication,
    StoredIssuedCreditNote,
    StoredIssuedCreditNoteVoid,
    StoredIssuedInvoiceVoid,
    StoredTenantApiCredential,
    StoredUnappliedCashApplication,
    StoredUnappliedCashRefund,
    StoredSpendBudget,
    StoredWebhookDeliveryAttempt,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_DISPUTE_HELD,
    EVENT_TYPE_DISPUTE_RELEASED,
    EVENT_TYPE_UNAPPLIED_CASH_APPLIED,
    EVENT_TYPE_REFUND_RECORDED,
    EVENT_TYPE_CREDIT_NOTE_APPLIED,
    EVENT_TYPE_CREDIT_NOTE_ISSUED,
    EVENT_TYPE_CREDIT_NOTE_VOIDED,
    EVENT_TYPE_INVOICE_VOIDED,
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    EVENT_TYPE_SPEND_BUDGET_APPROACHING,
    EVENT_TYPE_SPEND_BUDGET_OVER,
    EVENT_TYPE_SPEND_BUDGET_PUBLISHED,
    enqueue_accepted_fact,
)
from tests.test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW
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
DRAFT_ONLY_JOURNAL_WHERE = """
payment_receipt_id IS NULL
AND credit_adjustment_id IS NULL
AND collection_write_off_id IS NULL
AND unapplied_cash_refund_id IS NULL
AND unapplied_cash_id IS NULL
AND unapplied_cash_application_id IS NULL
AND issued_invoice_void_id IS NULL
AND issued_credit_note_void_id IS NULL
"""


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
        if len(applied) != 46:
            raise AssertionError(f"expected 46 migrations, got {len(applied)}")

    @classmethod
    def tearDownClass(cls) -> None:
        """Close the dedicated integration connection."""
        cls.connection.close()

    def setUp(self) -> None:
        """Reset only the dedicated test database tables and seed two tenants."""
        self.connection.execute(
            """
            TRUNCATE TABLE
                billing_core.reconciliation_run_line,
                billing_core.reconciliation_run,
                billing_core.reconciliation_evidence,
                billing_core.reconciliation_resolution,
                billing_core.reconciliation_exception,
                billing_core.reconciliation_line,
                billing_core.fx_conversion,
                billing_core.fx_rate,
                billing_core.billing_period_transition,
                billing_core.billing_period,
                billing_core.spend_budget,
                billing_core.journal_proposal_line,
                billing_core.journal_proposal,
                billing_core.collection_case_settlement,
                billing_core.collection_write_off,
                billing_core.webhook_delivery_attempt,
                billing_core.webhook_outbox_event,
                billing_core.webhook_subscription,
                billing_core.unapplied_cash_refund,
                billing_core.unapplied_cash_application,
                billing_core.unapplied_cash,
                billing_core.payment_receipt,
                billing_core.payment_intent,
                billing_core.issued_invoice_void,
                billing_core.issued_credit_note_void,
                billing_core.credit_note_application,
                billing_core.issued_credit_note,
                billing_core.credit_adjustment,
                billing_core.collection_dunning_event,
                billing_core.collection_dispute,
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

    def test_period_close_facts_are_durable_and_append_only(self) -> None:
        """Persist period, FX, and reconciliation facts with replay-safe identities."""
        period = create_billing_period(
            TENANT_ONE,
            CATALOG_START.date(),
            (CATALOG_START + timedelta(days=31)).date(),
            opened_by="operator:finance_001",
            opened_at=CATALOG_START,
            period_id=uuid4(),
        )
        self.assertEqual(self.ledger.insert_billing_period(period), period)
        with self.assertRaises(ValueError):
            self.ledger.insert_billing_period(
                replace(period, period_start=CATALOG_START.date() + timedelta(days=1))
            )
        soft_closed = period.advance(
            "soft_closed",
            actor_reference="operator:finance_002",
            authorization_reference="approval:period_001",
            reason="close usage window",
            transitioned_at=CATALOG_START + timedelta(hours=1),
            transition_id=uuid4(),
        )
        self.assertEqual(self.ledger.insert_billing_period(soft_closed), soft_closed)
        reconciled = soft_closed.advance(
            "reconciled",
            actor_reference="operator:finance_005",
            authorization_reference="approval:period_003",
            reason="reconcile usage window",
            transitioned_at=CATALOG_START + timedelta(hours=1),
            transition_id=uuid4(),
        )
        self.assertEqual(self.ledger.insert_billing_period(reconciled), reconciled)
        invoiced = reconciled.advance(
            "invoiced",
            actor_reference="operator:finance_006",
            authorization_reference="approval:period_004",
            reason="issue invoice",
            transitioned_at=CATALOG_START + timedelta(hours=2),
            transition_id=uuid4(),
        )
        hard_closed = invoiced.advance(
            "hard_closed",
            actor_reference="operator:finance_007",
            authorization_reference="approval:period_005",
            reason="finalize period",
            transitioned_at=CATALOG_START + timedelta(hours=3),
            transition_id=uuid4(),
        )
        self.assertEqual(self.ledger.insert_billing_period(hard_closed), hard_closed)
        self.assertEqual(self.ledger.get_billing_period(TENANT_ONE, period.period_id), hard_closed)
        self.assertIsNone(self.ledger.get_billing_period(TENANT_ONE, uuid4()))
        self.assertIsNone(self.ledger.get_billing_period(TENANT_TWO, period.period_id))
        second_period = create_billing_period(
            TENANT_ONE,
            CATALOG_START.date(),
            (CATALOG_START + timedelta(days=31)).date(),
            opened_by="operator:finance_003",
            opened_at=CATALOG_START,
            period_id=uuid4(),
        ).advance(
            "soft_closed",
            actor_reference="operator:finance_004",
            authorization_reference="approval:period_002",
            reason="close second usage window",
            transitioned_at=CATALOG_START + timedelta(hours=2),
            transition_id=soft_closed.transitions[0].transition_id,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_billing_period(second_period)
        self.assertEqual(self.ledger.list_billing_periods(TENANT_TWO), ())
        self.assertEqual(self.ledger.list_billing_periods(TENANT_ONE), (hard_closed,))
        with self.assertRaises(ValueError):
            self.ledger.insert_billing_period(
                replace(
                    hard_closed,
                    transitions=(
                        replace(hard_closed.transitions[0], reason="rewrite"),
                        *hard_closed.transitions[1:],
                    )
                )
            )

        rate = create_fx_rate(
            "provider:fx_001",
            "provider",
            "USD",
            "KRW",
            "1350.1234",
            4,
            CATALOG_START,
            CATALOG_START + timedelta(minutes=1),
            fx_rate_id=uuid4(),
        )
        self.assertEqual(self.ledger.insert_fx_rate(rate), rate)
        self.assertEqual(self.ledger.insert_fx_rate(rate), rate)
        self.assertEqual(self.ledger.get_fx_rate(rate.fx_rate_id), rate)
        self.assertIsNone(self.ledger.get_fx_rate(uuid4()))
        with self.assertRaises(ValueError):
            self.ledger.insert_fx_rate(replace(rate, rate_source="provider:other"))
        conversion = convert_currency_amount(
            "10.25",
            "USD",
            0,
            rate,
            fx_conversion_id=uuid4(),
            converted_at=CATALOG_START + timedelta(minutes=2),
        )
        self.assertEqual(self.ledger.insert_fx_conversion(conversion), conversion)
        self.assertIsNone(self.ledger.get_fx_conversion(uuid4()))
        self.assertEqual(self.ledger.get_fx_conversion(conversion.fx_conversion_id), conversion)
        with self.assertRaises(KeyError):
            self.ledger.insert_fx_conversion(replace(conversion, fx_rate_id=uuid4()))
        with self.assertRaises(ValueError):
            self.ledger.insert_fx_conversion(replace(conversion, source_currency="EUR"))
        with self.assertRaises(ValueError):
            self.ledger.insert_fx_conversion(
                replace(
                    conversion,
                    source_amount=Decimal("10"),
                    quote_amount=Decimal("13501"),
                )
            )

        matched = assess_reconciliation_line(
            period.period_id,
            "provider:account_001",
            "USD",
            "10",
            "10",
            "10",
            assessed_at=CATALOG_START + timedelta(minutes=3),
            reconciliation_line_id=uuid4(),
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
        )
        exception = assess_reconciliation_line(
            period.period_id,
            "provider:account_001",
            "USD",
            "10",
            "9",
            "9",
            assessed_at=CATALOG_START + timedelta(minutes=4),
            reconciliation_line_id=uuid4(),
            internal_currency_code="USD",
            provider_currency_code="EUR",
            cash_currency_code="USD",
        )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_line(TENANT_TWO, matched)
        self.assertEqual(self.ledger.insert_reconciliation_line(TENANT_ONE, matched), matched)
        self.assertEqual(self.ledger.insert_reconciliation_line(TENANT_ONE, exception), exception)
        self.assertEqual(self.ledger.insert_reconciliation_line(TENANT_ONE, exception), exception)
        changed_exception_list = replace(
            exception,
            exceptions=exception.exceptions
            + (
                ReconciliationException(
                    ReconciliationExceptionCode.QUANTITY_MISMATCH,
                    _NEXT_ACTIONS[ReconciliationExceptionCode.QUANTITY_MISMATCH],
                ),
            ),
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_reconciliation_line(TENANT_ONE, changed_exception_list)
        self.assertEqual(
            self.ledger.get_reconciliation_line(TENANT_ONE, exception.reconciliation_line_id),
            exception,
        )
        expanded_exception = ReconciliationLine(
            reconciliation_line_id=uuid4(),
            period_id=period.period_id,
            provider_account_reference="provider:account_001",
            currency_code="USD",
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
            internal_expected_amount=Decimal("10"),
            provider_actual_amount=Decimal("9"),
            cash_actual_amount=Decimal("9"),
            provider_fee_amount=Decimal("0"),
            withheld_tax_amount=Decimal("0"),
            reserve_amount=Decimal("0"),
            expected_cash_amount=Decimal("9"),
            status=ReconciliationLineStatus.EXCEPTION,
            exceptions=(
                ReconciliationException(
                    ReconciliationExceptionCode.TAX_MISMATCH,
                    _NEXT_ACTIONS[ReconciliationExceptionCode.TAX_MISMATCH],
                ),
            ),
            assessed_at=CATALOG_START + timedelta(minutes=4, seconds=1),
            reconciliation_line_contract_version=1,
        )
        self.assertEqual(
            self.ledger.insert_reconciliation_line(TENANT_ONE, expanded_exception), expanded_exception
        )
        self.assertEqual(
            self.ledger.get_reconciliation_line(
                TENANT_ONE, expanded_exception.reconciliation_line_id
            ),
            expanded_exception,
        )
        evidence = ReconciliationEvidence(
            evidence_id=uuid4(),
            reconciliation_line_id=expanded_exception.reconciliation_line_id,
            exception_code=ReconciliationExceptionCode.TAX_MISMATCH,
            evidence_kind="provider_tax_document",
            evidence_reference="urn:cwl:evidence:provider-tax-001",
            evidence_sha256="sha256:" + "b" * 64,
            captured_by="operator:finance_001",
            captured_at=CATALOG_START + timedelta(minutes=4, seconds=2),
        )
        self.assertEqual(validate_reconciliation_evidence(evidence.as_contract_dict()), ())
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_evidence(TENANT_TWO, evidence)
        self.assertEqual(self.ledger.insert_reconciliation_evidence(TENANT_ONE, evidence), evidence)
        self.assertEqual(self.ledger.insert_reconciliation_evidence(TENANT_ONE, evidence), evidence)
        self.assertEqual(
            self.ledger.get_reconciliation_evidence(TENANT_ONE, evidence.evidence_id),
            evidence,
        )
        self.assertIsNone(self.ledger.get_reconciliation_evidence(TENANT_ONE, uuid4()))
        self.assertIsNone(
            self.ledger.get_reconciliation_evidence(TENANT_TWO, evidence.evidence_id)
        )
        self.assertEqual(
            self.ledger.list_reconciliation_evidence(
                TENANT_ONE, reconciliation_line_id=expanded_exception.reconciliation_line_id
            ),
            (evidence,),
        )
        self.assertEqual(self.ledger.list_reconciliation_evidence(TENANT_TWO), ())
        with self.assertRaises(ValueError):
            self.ledger.insert_reconciliation_evidence(
                TENANT_ONE,
                replace(evidence, evidence_reference="urn:cwl:evidence:rewrite")
            )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_evidence(
                TENANT_ONE,
                replace(
                    evidence,
                    evidence_id=uuid4(),
                    exception_code=ReconciliationExceptionCode.REFUND_MISMATCH,
                )
            )
        run = ReconciliationRun(
            run_id=uuid4(),
            period_id=period.period_id,
            started_at=CATALOG_START + timedelta(minutes=7),
            completed_at=CATALOG_START + timedelta(minutes=8),
            reconciliation_line_ids=(
                matched.reconciliation_line_id,
                exception.reconciliation_line_id,
                expanded_exception.reconciliation_line_id,
            ),
            blocking_exception_count=3,
        )
        self.assertEqual(validate_reconciliation_run(run.as_contract_dict()), ())
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_run(TENANT_TWO, run)
        self.assertEqual(self.ledger.insert_reconciliation_run(TENANT_ONE, run), run)
        self.assertEqual(self.ledger.insert_reconciliation_run(TENANT_ONE, run), run)
        self.assertEqual(self.ledger.get_reconciliation_run(TENANT_ONE, run.run_id), run)
        self.assertIsNone(self.ledger.get_reconciliation_run(TENANT_ONE, uuid4()))
        self.assertIsNone(self.ledger.get_reconciliation_run(TENANT_TWO, run.run_id))
        self.assertEqual(self.ledger.list_reconciliation_runs(TENANT_TWO), ())
        self.assertEqual(
            self.ledger.list_reconciliation_runs(TENANT_ONE, period_id=period.period_id),
            (run,),
        )
        empty_run = ReconciliationRun(
            run_id=uuid4(),
            period_id=period.period_id,
            started_at=CATALOG_START + timedelta(minutes=9),
            completed_at=CATALOG_START + timedelta(minutes=10),
            reconciliation_line_ids=(),
            blocking_exception_count=0,
        )
        self.assertEqual(self.ledger.insert_reconciliation_run(TENANT_ONE, empty_run), empty_run)
        with self.assertRaises(ValueError):
            self.ledger.insert_reconciliation_run(
                TENANT_ONE,
                replace(run, blocking_exception_count=4)
            )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_run(
                TENANT_ONE,
                replace(run, run_id=uuid4(), reconciliation_line_ids=(uuid4(),))
            )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_run(
                TENANT_ONE,
                replace(run, run_id=uuid4(), period_id=uuid4())
            )
        self.assertIsNone(self.ledger.get_reconciliation_line(TENANT_ONE, uuid4()))
        self.assertEqual(
            {line.reconciliation_line_id for line in self.ledger.list_reconciliation_lines(TENANT_ONE)},
            {
                matched.reconciliation_line_id,
                exception.reconciliation_line_id,
                expanded_exception.reconciliation_line_id,
            },
        )
        self.assertEqual(
            self.ledger.list_reconciliation_lines(
                TENANT_ONE, period_id=period.period_id
            ),
            (matched, exception, expanded_exception),
        )
        self.assertEqual(
            {
                item.exception_code
                for item in self.ledger.get_reconciliation_line(
                    TENANT_ONE, exception.reconciliation_line_id
                ).exceptions
            },
            {
                ReconciliationExceptionCode.CURRENCY_MISMATCH,
                ReconciliationExceptionCode.PRICE_MISMATCH,
            },
        )
        resolution = ReconciliationResolution(
            resolution_id=uuid4(),
            reconciliation_line_id=exception.reconciliation_line_id,
            exception_code=ReconciliationExceptionCode.CURRENCY_MISMATCH,
            resolution_status=ReconciliationResolutionStatus.WAIVED,
            owner_reference="operator:finance_008",
            resolution_reason="provider contract is authoritative for this payout",
            evidence_reference="urn:cwl:evidence:provider-payout-001",
            maker_reference="operator:finance_008",
            checker_reference="operator:finance_009",
            resolved_at=CATALOG_START + timedelta(minutes=6),
        )
        self.assertEqual(validate_reconciliation_resolution(resolution.as_contract_dict()), ())
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_resolution(TENANT_TWO, resolution)
        self.assertEqual(self.ledger.insert_reconciliation_resolution(TENANT_ONE, resolution), resolution)
        self.assertEqual(self.ledger.insert_reconciliation_resolution(TENANT_ONE, resolution), resolution)
        self.assertEqual(
            self.ledger.get_reconciliation_resolution(TENANT_ONE, resolution.resolution_id),
            resolution,
        )
        self.assertIsNone(self.ledger.get_reconciliation_resolution(TENANT_ONE, uuid4()))
        self.assertIsNone(
            self.ledger.get_reconciliation_resolution(TENANT_TWO, resolution.resolution_id)
        )
        self.assertEqual(
            self.ledger.list_reconciliation_resolutions(
                TENANT_ONE, reconciliation_line_id=exception.reconciliation_line_id
            ),
            (resolution,),
        )
        self.assertEqual(self.ledger.list_reconciliation_resolutions(TENANT_TWO), ())
        with self.assertRaises(ValueError):
            self.ledger.insert_reconciliation_resolution(
                TENANT_ONE,
                replace(resolution, resolution_reason="rewrite")
            )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_resolution(
                TENANT_ONE,
                replace(resolution, resolution_id=uuid4(), reconciliation_line_id=matched.reconciliation_line_id)
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_reconciliation_line(
                TENANT_ONE,
                replace(matched, provider_account_reference="provider:other")
            )
        missing_period_line = assess_reconciliation_line(
            uuid4(),
            "provider:account_001",
            "USD",
            "1",
            "1",
            "1",
            assessed_at=CATALOG_START + timedelta(minutes=5),
            reconciliation_line_id=uuid4(),
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
        )
        with self.assertRaises(KeyError):
            self.ledger.insert_reconciliation_line(TENANT_ONE, missing_period_line)
        immutable_mutations = (
            (
                "UPDATE billing_core.billing_period_transition "
                "SET transition_reason = 'rewrite' WHERE transition_id = %s",
                (hard_closed.transitions[0].transition_id,),
            ),
            (
                "DELETE FROM billing_core.billing_period_transition WHERE transition_id = %s",
                (hard_closed.transitions[0].transition_id,),
            ),
            (
                "UPDATE billing_core.billing_period SET opened_by = 'rewrite' WHERE period_id = %s",
                (period.period_id,),
            ),
            (
                "DELETE FROM billing_core.billing_period WHERE period_id = %s",
                (period.period_id,),
            ),
            (
                "UPDATE billing_core.fx_rate SET rate_source = 'rewrite' WHERE fx_rate_id = %s",
                (rate.fx_rate_id,),
            ),
            (
                "DELETE FROM billing_core.fx_rate WHERE fx_rate_id = %s",
                (rate.fx_rate_id,),
            ),
            (
                "UPDATE billing_core.fx_conversion SET source_currency = 'EUR' "
                "WHERE fx_conversion_id = %s",
                (conversion.fx_conversion_id,),
            ),
            (
                "DELETE FROM billing_core.fx_conversion WHERE fx_conversion_id = %s",
                (conversion.fx_conversion_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_line "
                "SET provider_account_reference = 'provider:rewrite' "
                "WHERE reconciliation_line_id = %s",
                (matched.reconciliation_line_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_exception SET next_action = 'rewrite' "
                "WHERE reconciliation_line_id = %s AND exception_number = 1",
                (exception.reconciliation_line_id,),
            ),
            (
                "DELETE FROM billing_core.reconciliation_exception "
                "WHERE reconciliation_line_id = %s AND exception_number = 1",
                (exception.reconciliation_line_id,),
            ),
            (
                "DELETE FROM billing_core.reconciliation_line WHERE reconciliation_line_id = %s",
                (matched.reconciliation_line_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_evidence "
                "SET evidence_reference = 'urn:cwl:evidence:rewrite' WHERE evidence_id = %s",
                (evidence.evidence_id,),
            ),
            (
                "DELETE FROM billing_core.reconciliation_evidence WHERE evidence_id = %s",
                (evidence.evidence_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_resolution "
                "SET resolution_reason = 'rewrite' WHERE resolution_id = %s",
                (resolution.resolution_id,),
            ),
            (
                "DELETE FROM billing_core.reconciliation_resolution WHERE resolution_id = %s",
                (resolution.resolution_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_run SET blocking_exception_count = 4 "
                "WHERE run_id = %s",
                (run.run_id,),
            ),
            (
                "DELETE FROM billing_core.reconciliation_run WHERE run_id = %s",
                (empty_run.run_id,),
            ),
            (
                "UPDATE billing_core.reconciliation_run_line SET reconciliation_line_id = %s "
                "WHERE run_id = %s AND line_number = 1",
                (exception.reconciliation_line_id, run.run_id),
            ),
            (
                "DELETE FROM billing_core.reconciliation_run_line "
                "WHERE run_id = %s AND line_number = 1",
                (run.run_id,),
            ),
        )
        for statement, parameters in immutable_mutations:
            with self.subTest(statement=statement.split()[0:2]):
                with self.assertRaises(psycopg.errors.RaiseException):
                    with self.connection.transaction():
                        self.connection.execute(statement, parameters)
        with self.assertRaises(KeyError):
            self.ledger.list_reconciliation_lines("urn:cwl:missing_tenant")

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

    def test_tenant_api_credentials_are_durable_tenant_scoped_and_revocable(self) -> None:
        """Issue-once secrets survive reload and authorized reads stay tenant-scoped."""
        service = TenantApiCredentialService(self.ledger)
        first = service.issue_credential(TENANT_ONE, "operator_key")
        self.assertEqual(first.tenant_api_credential_outcome_code.value, "accepted")
        secret = first.api_credential_secret
        assert secret is not None
        TenantApiCredentialService(self.ledger).authorize_request(TENANT_ONE, secret)
        with self.assertRaises(TenantApiCredentialQueryError):
            TenantApiCredentialService(self.ledger).authorize_request(
                TENANT_ONE, "cwlak_not_a_stored_secret"
            )

        stored_first = self.ledger.get_tenant_api_credential(first.tenant_api_credential_id)
        assert stored_first is not None
        self.assertEqual(stored_first.credential_status, "active")
        self.assertIsNone(stored_first.revoked_at)
        self.assertIsNone(self.ledger.get_tenant_api_credential(uuid4()))
        self.assertIsNone(self.ledger.find_tenant_api_credential_by_hash("hmac-sha256:" + "0" * 64))
        self.assertEqual(
            self.ledger.find_tenant_api_credential_by_hash(stored_first.credential_secret_hash),
            stored_first,
        )

        tenant_id = self.ledger.require_tenant(TENANT_ONE).tenant_account_id
        other_tenant_id = self.ledger.require_tenant(TENANT_TWO).tenant_account_id
        self.assertEqual(self.ledger.list_tenant_api_credentials(other_tenant_id), ())
        self.assertEqual(self.ledger.list_tenant_api_credentials(tenant_id), (stored_first,))
        self.assertEqual(
            self.ledger.list_active_tenant_api_credentials(tenant_id), (stored_first,)
        )

        def make_stored(**overrides: object) -> StoredTenantApiCredential:
            base: dict[str, object] = {
                "tenant_api_credential_id": uuid4(),
                "tenant_account_id": tenant_id,
                "tenant_api_credential_contract_version": 1,
                "credential_label": "operator_key",
                "credential_prefix": "cwlak_prefix1",
                "credential_secret_hash": hash_api_credential_secret(
                    f"cwlak_{uuid4().hex}", "test_pepper_two"
                ),
                "credential_status": "active",
                "issued_at": datetime(2026, 8, 20, tzinfo=UTC),
                "revoked_at": None,
            }
            base.update(overrides)
            return StoredTenantApiCredential(**base)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            self.ledger.insert_tenant_api_credential(make_stored(credential_status="expired"))
        with self.assertRaises(ValueError):
            self.ledger.insert_tenant_api_credential(make_stored(credential_secret_hash="plaintext"))
        with self.assertRaises(ValueError):
            self.ledger.insert_tenant_api_credential(
                make_stored(credential_secret_hash=stored_first.credential_secret_hash)
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_tenant_api_credential(
                make_stored(tenant_api_credential_id=first.tenant_api_credential_id)
            )
        direct = make_stored()
        self.assertEqual(self.ledger.insert_tenant_api_credential(direct), direct)

        revoke_one_at = datetime.now(UTC)
        revoke_two_at = datetime.now(UTC)
        with self.assertRaises(ValueError):
            self.ledger.revoke_tenant_api_credential(uuid4(), revoke_two_at)
        revoked = self.ledger.revoke_tenant_api_credential(
            first.tenant_api_credential_id, revoke_one_at
        )
        self.assertEqual(revoked.credential_status, "revoked")
        assert revoked.revoked_at is not None
        again = self.ledger.revoke_tenant_api_credential(
            first.tenant_api_credential_id, revoke_two_at
        )
        self.assertEqual(again, revoked)
        self.assertEqual(
            self.ledger.list_tenant_api_credentials(tenant_id), (direct, revoked)
        )
        self.assertEqual(self.ledger.list_active_tenant_api_credentials(tenant_id), (direct,))

    def test_threaded_requests_serialize_on_one_connection_safely(self) -> None:
        """Concurrent web-tier workers never interleave one connection's transactions."""
        from metering_billing.http_app import create_http_app
        from tests.test_http_app_backend_selection import invoke_http

        app = create_http_app(ledger=self.ledger)
        service = TenantApiCredentialService(self.ledger)
        first = service.issue_credential(TENANT_ONE, "operator_key")
        secret = first.api_credential_secret
        assert secret is not None

        failures: list[BaseException] = []
        barrier = Barrier(8)

        def hammer(worker_number: int) -> None:
            try:
                barrier.wait()
                for _ in range(20):
                    status, body = invoke_http(
                        app,
                        "GET",
                        "/v1/tenant-api-credentials",
                        headers={
                            "X-CWL-Tenant-Reference": TENANT_ONE,
                            "X-CWL-Api-Key": secret,
                        },
                    )
                    if status != 200:
                        raise AssertionError(f"unexpected status {status}: {body}")
                    self.ledger.migration_history_row_count()
            except BaseException as error:  # noqa: BLE001 - collected below
                failures.append(error)

        with ThreadPoolExecutor(max_workers=8) as executor:
            tuple(executor.map(hammer, range(8)))
        self.assertEqual(failures, [])

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

    def test_write_off_journal_is_durable(self) -> None:
        """Persist one write-off journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        remaining = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(remaining, leftover)
        self.assertNotEqual(remaining, KNOWN_MORNING_TOTAL)
        write_off = CollectionWriteOffService(
            self.ledger, clock=lambda: CATALOG_START
        ).write_off_collection_case(TENANT_ONE, opened.collection_case_id)
        self.assertEqual(write_off.collection_write_off_outcome_code.value, "accepted")
        assert write_off.collection_write_off_id is not None
        stored_write_off = self.ledger.get_collection_write_off(write_off.collection_write_off_id)
        assert stored_write_off is not None
        self.assertEqual(stored_write_off.write_off_amount, remaining)
        self.assertNotEqual(stored_write_off.write_off_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(stored_write_off.remaining_outstanding_amount, Decimal("0"))
        zero_case = self.ledger.get_collection_case(opened.collection_case_id)
        assert zero_case is not None
        self.assertEqual(zero_case.outstanding_amount, Decimal("0.000000000000"))
        self.assertEqual(zero_case.collection_case_status, "open")
        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_write_off_journal(TENANT_ONE, write_off.collection_write_off_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.collection_write_off_id, write_off.collection_write_off_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 2)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "write_off_expense")
        self.assertEqual(stored.proposal_lines[0].debit_amount, remaining)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "accounts_receivable")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, remaining)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_write_off(
                stored.tenant_account_id, write_off.collection_write_off_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_write_off(stored.tenant_account_id, uuid4())
        )
        write_off_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.collection_write_off_id is not None
        )
        self.assertEqual(write_off_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        remaining_after = self.ledger.get_collection_case(opened.collection_case_id)
        assert remaining_after is not None
        self.assertEqual(remaining_after.outstanding_amount, Decimal("0.000000000000"))

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_write_off_journal(TENANT_ONE, write_off.collection_write_off_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, remaining)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        still_zero = self.ledger.get_collection_case(opened.collection_case_id)
        assert still_zero is not None
        self.assertEqual(still_zero.outstanding_amount, Decimal("0.000000000000"))

        rejected = AccountingExportService(self.ledger).propose_write_off_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_write_off_journal(
            TENANT_TWO, write_off.collection_write_off_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        leftover_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf71c",
            source_event_key="workflow_381:step_14:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(leftover_usage)
        leftover_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        leftover_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, leftover_window, 1, rate_card_code="cwl_standard"
        )
        leftover_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, leftover_rating.rating_run_id
        )
        leftover_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, leftover_draft.invoice_draft_id)
        assert leftover_opened.collection_case_id is not None
        leftover_remaining = self.ledger.get_collection_case(
            leftover_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(leftover_remaining, leftover)
        leftover_write_off = CollectionWriteOffService(
            self.ledger, clock=lambda: CATALOG_START
        ).write_off_collection_case(TENANT_ONE, leftover_opened.collection_case_id)
        self.assertEqual(leftover_write_off.collection_write_off_outcome_code.value, "accepted")
        assert leftover_write_off.collection_write_off_id is not None
        leftover_stored_write_off = self.ledger.get_collection_write_off(
            leftover_write_off.collection_write_off_id
        )
        assert leftover_stored_write_off is not None
        self.assertEqual(leftover_stored_write_off.write_off_amount, leftover)
        self.assertEqual(leftover_stored_write_off.remaining_outstanding_amount, Decimal("0"))
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_write_off(
                leftover_stored_write_off.tenant_account_id,
                leftover_write_off.collection_write_off_id,
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf81c",
            source_event_key="workflow_381:step_15:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_opened.collection_case_id is not None
        crash_write_off = CollectionWriteOffService(
            self.ledger, clock=lambda: CATALOG_START
        ).write_off_collection_case(TENANT_ONE, crash_opened.collection_case_id)
        assert crash_write_off.collection_write_off_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        ).propose_write_off_journal(TENANT_ONE, crash_write_off.collection_write_off_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_write_off_journal(TENANT_ONE, crash_write_off.collection_write_off_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(stored, payment_receipt_id=None, credit_adjustment_id=None, collection_write_off_id=None),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    collection_write_off_id=leftover_write_off.collection_write_off_id,
                ),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
        ).propose_write_off_journal(TENANT_ONE, leftover_write_off.collection_write_off_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(later_stored.collection_write_off_id, leftover_write_off.collection_write_off_id)
        self.assertEqual(later_stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, remaining)
        self.assertEqual(presentment.collection_write_off_id, write_off.collection_write_off_id)
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.collection_write_off_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        leftover_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(leftover_presentment.proposal_lines[0].debit_amount, leftover)
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_write_off(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_write_off_journal(TENANT_ONE, write_off.collection_write_off_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE collection_write_off_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

    def test_unapplied_cash_journal_is_durable(self) -> None:
        """Persist one leftover journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        remaining_before = received
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None
        stored_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert stored_leftover is not None
        self.assertEqual(stored_leftover.unapplied_amount, leftover)
        self.assertEqual(stored_leftover.unapplied_cash_status, "parked")
        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_journal(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.unapplied_cash_id, parked.unapplied_cash_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 2)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "cash_receipt")
        self.assertEqual(stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "unapplied_cash")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, leftover)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_unapplied_cash(
                stored.tenant_account_id, parked.unapplied_cash_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_unapplied_cash(stored.tenant_account_id, uuid4())
        )
        leftover_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.unapplied_cash_id is not None
        )
        self.assertEqual(leftover_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_parked = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert still_parked is not None
        self.assertEqual(still_parked.unapplied_amount, leftover)
        self.assertEqual(still_parked.unapplied_cash_status, "parked")
        remaining_after = self.ledger.get_collection_case(opened.collection_case_id)
        assert remaining_after is not None
        self.assertEqual(remaining_after.outstanding_amount, remaining_before - received)

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_journal(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        still_parked_replay = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert still_parked_replay is not None
        self.assertEqual(still_parked_replay.unapplied_amount, leftover)

        rejected = AccountingExportService(self.ledger).propose_unapplied_cash_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_unapplied_cash_journal(
            TENANT_TWO, parked.unapplied_cash_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf71c",
            source_event_key="workflow_381:step_14:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        later_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, later_draft.invoice_draft_id)
        assert later_opened.collection_case_id is not None
        later_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, later_opened.collection_case_id)
        assert later_intent.payment_intent_id is not None
        later_received = self.ledger.get_collection_case(
            later_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(later_received, leftover)
        later_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, later_intent.payment_intent_id, later_received
        )
        assert later_receipt.payment_receipt_id is not None
        later_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, later_receipt.payment_receipt_id, leftover)
        self.assertEqual(later_parked.unapplied_cash_outcome_code.value, "accepted")
        assert later_parked.unapplied_cash_id is not None
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_unapplied_cash(
                stored.tenant_account_id, later_parked.unapplied_cash_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf81c",
            source_event_key="workflow_381:step_15:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_opened.collection_case_id is not None
        crash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, crash_opened.collection_case_id)
        assert crash_intent.payment_intent_id is not None
        crash_received = self.ledger.get_collection_case(
            crash_opened.collection_case_id
        ).outstanding_amount
        crash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, crash_intent.payment_intent_id, crash_received
        )
        assert crash_receipt.payment_receipt_id is not None
        crash_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, crash_receipt.payment_receipt_id, leftover)
        assert crash_parked.unapplied_cash_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 30, tzinfo=UTC)
        ).propose_unapplied_cash_journal(TENANT_ONE, crash_parked.unapplied_cash_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_journal(TENANT_ONE, crash_parked.unapplied_cash_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    payment_receipt_id=None,
                    credit_adjustment_id=None,
                    collection_write_off_id=None,
                    unapplied_cash_id=None,
                ),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(stored, unapplied_cash_id=later_parked.unapplied_cash_id),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_unapplied_cash_journal(TENANT_ONE, later_parked.unapplied_cash_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(later_stored.unapplied_cash_id, later_parked.unapplied_cash_id)
        self.assertEqual(later_stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(presentment.unapplied_cash_id, parked.unapplied_cash_id)
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.unapplied_cash_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(later_presentment.proposal_lines[0].debit_amount, leftover)
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_unapplied_cash(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_unapplied_cash_journal(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

    def test_unapplied_cash_application_journal_is_durable(self) -> None:
        """Persist one leftover-apply journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        second_case_amount = Decimal("20.00")
        leftover_apply_remaining = Decimal("19.999")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None
        stored_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert stored_leftover is not None
        self.assertEqual(stored_leftover.unapplied_amount, leftover)
        self.assertEqual(stored_leftover.unapplied_cash_status, "parked")

        twenty_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfc1c",
            source_event_key="workflow_381:step_20:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(twenty_usage)
        twenty_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        twenty_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, twenty_window, 1, rate_card_code="cwl_standard"
        )
        twenty_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, twenty_rating.rating_run_id
        )
        twenty_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, twenty_draft.invoice_draft_id)
        assert twenty_opened.collection_case_id is not None
        twenty_remaining = self.ledger.get_collection_case(
            twenty_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(twenty_remaining, second_case_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, twenty_opened.collection_case_id
        )
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert applied.unapplied_cash_application_id is not None
        self.assertEqual(applied.remaining_outstanding_amount, leftover_apply_remaining)
        applied_case = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(applied_case.collection_case_status, "open")

        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, applied.unapplied_cash_application_id
        )
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(
            stored.unapplied_cash_application_id, applied.unapplied_cash_application_id
        )
        self.assertEqual(stored.invoice_draft_id, twenty_draft.invoice_draft_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 2)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "unapplied_cash")
        self.assertEqual(stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "accounts_receivable")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, leftover)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_unapplied_cash_application(
                stored.tenant_account_id, applied.unapplied_cash_application_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_unapplied_cash_application(
                stored.tenant_account_id, uuid4()
            )
        )
        apply_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.unapplied_cash_application_id is not None
        )
        self.assertEqual(apply_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_applied = self.ledger.get_unapplied_cash_application(
            applied.unapplied_cash_application_id
        )
        assert still_applied is not None
        self.assertEqual(still_applied.applied_amount, leftover)
        still_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert still_remaining is not None
        self.assertEqual(still_remaining.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(still_remaining.collection_case_status, "open")
        still_parked = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert still_parked is not None
        self.assertEqual(still_parked.unapplied_amount, leftover)
        self.assertEqual(still_parked.unapplied_cash_status, "parked")

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, applied.unapplied_cash_application_id
        )
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        replay_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert replay_remaining is not None
        self.assertEqual(replay_remaining.outstanding_amount, leftover_apply_remaining)

        rejected = AccountingExportService(self.ledger).propose_unapplied_cash_application_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_unapplied_cash_application_journal(
            TENANT_TWO, applied.unapplied_cash_application_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfd1c",
            source_event_key="workflow_381:step_21:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        later_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, later_draft.invoice_draft_id)
        assert later_opened.collection_case_id is not None
        later_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, later_opened.collection_case_id)
        assert later_intent.payment_intent_id is not None
        later_received = self.ledger.get_collection_case(
            later_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(later_received, leftover)
        later_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, later_intent.payment_intent_id, later_received
        )
        assert later_receipt.payment_receipt_id is not None
        later_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, later_receipt.payment_receipt_id, leftover)
        self.assertEqual(later_parked.unapplied_cash_outcome_code.value, "accepted")
        assert later_parked.unapplied_cash_id is not None
        later_target_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfe1c",
            source_event_key="workflow_381:step_22:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_target_usage)
        later_target_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        later_target_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_target_window, 1, rate_card_code="cwl_standard"
        )
        later_target_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_target_rating.rating_run_id
        )
        later_target = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, later_target_draft.invoice_draft_id)
        assert later_target.collection_case_id is not None
        later_applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 15, tzinfo=UTC)
        ).apply_unapplied_cash(
            TENANT_ONE, later_parked.unapplied_cash_id, later_target.collection_case_id
        )
        self.assertEqual(later_applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert later_applied.unapplied_cash_application_id is not None
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_unapplied_cash_application(
                stored.tenant_account_id, later_applied.unapplied_cash_application_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bff1c",
            source_event_key="workflow_381:step_23:attempt_01",
            occurred_at="2026-08-16T16:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T16:00:00Z", "2026-08-16T17:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_opened.collection_case_id is not None
        crash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, crash_opened.collection_case_id)
        assert crash_intent.payment_intent_id is not None
        crash_received = self.ledger.get_collection_case(
            crash_opened.collection_case_id
        ).outstanding_amount
        crash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, crash_intent.payment_intent_id, crash_received
        )
        assert crash_receipt.payment_receipt_id is not None
        crash_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, crash_receipt.payment_receipt_id, leftover)
        assert crash_parked.unapplied_cash_id is not None
        crash_target_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4c001c",
            source_event_key="workflow_381:step_24:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_target_usage)
        crash_target_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        crash_target_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_target_window, 1, rate_card_code="cwl_standard"
        )
        crash_target_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_target_rating.rating_run_id
        )
        crash_target = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_target_draft.invoice_draft_id)
        assert crash_target.collection_case_id is not None
        crash_applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 15, tzinfo=UTC)
        ).apply_unapplied_cash(
            TENANT_ONE, crash_parked.unapplied_cash_id, crash_target.collection_case_id
        )
        self.assertEqual(crash_applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert crash_applied.unapplied_cash_application_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 30, tzinfo=UTC)
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, crash_applied.unapplied_cash_application_id
        )
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, crash_applied.unapplied_cash_application_id
        )
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        healed_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert healed_remaining is not None
        self.assertEqual(healed_remaining.outstanding_amount, leftover_apply_remaining)

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    payment_receipt_id=None,
                    credit_adjustment_id=None,
                    collection_write_off_id=None,
                    unapplied_cash_id=None,
                    unapplied_cash_application_id=None,
                ),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    unapplied_cash_application_id=later_applied.unapplied_cash_application_id,
                ),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, later_applied.unapplied_cash_application_id
        )
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(
            later_stored.unapplied_cash_application_id,
            later_applied.unapplied_cash_application_id,
        )
        self.assertEqual(later_stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            presentment.unapplied_cash_application_id, applied.unapplied_cash_application_id
        )
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.unapplied_cash_application_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(later_presentment.proposal_lines[0].debit_amount, leftover)
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        fresh_remaining = PostgresUsageLedger(self.connection).get_collection_case(
            twenty_opened.collection_case_id
        )
        assert fresh_remaining is not None
        self.assertEqual(fresh_remaining.outstanding_amount, leftover_apply_remaining)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_unapplied_cash_application(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_unapplied_cash_application_journal(
            TENANT_ONE, applied.unapplied_cash_application_id
        )
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_application_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )
        raced_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert raced_remaining is not None
        self.assertEqual(raced_remaining.outstanding_amount, leftover_apply_remaining)

    def test_unapplied_cash_refund_journal_is_durable(self) -> None:
        """Persist one leftover-refund journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        second_case_amount = Decimal("20.00")
        leftover_apply_remaining = Decimal("19.999")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None

        twenty_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4d011c",
            source_event_key="workflow_381:step_30:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(twenty_usage)
        twenty_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        twenty_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, twenty_window, 1, rate_card_code="cwl_standard"
        )
        twenty_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, twenty_rating.rating_run_id
        )
        twenty_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, twenty_draft.invoice_draft_id)
        assert twenty_opened.collection_case_id is not None
        twenty_remaining = self.ledger.get_collection_case(
            twenty_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(twenty_remaining, second_case_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, twenty_opened.collection_case_id
        )
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert applied.unapplied_cash_application_id is not None
        self.assertEqual(applied.remaining_outstanding_amount, leftover_apply_remaining)
        applied_case = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(applied_case.collection_case_status, "open")

        refund_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4d021c",
            source_event_key="workflow_381:step_31:attempt_01",
            occurred_at="2026-08-16T12:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(refund_usage)
        refund_window = TimeWindow.from_iso8601(
            "2026-08-16T12:00:00Z", "2026-08-16T13:00:00Z"
        )
        refund_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, refund_window, 1, rate_card_code="cwl_standard"
        )
        refund_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, refund_rating.rating_run_id
        )
        refund_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, refund_draft.invoice_draft_id)
        assert refund_opened.collection_case_id is not None
        refund_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, refund_opened.collection_case_id)
        assert refund_intent.payment_intent_id is not None
        refund_received = self.ledger.get_collection_case(
            refund_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(refund_received, leftover)
        refund_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, refund_intent.payment_intent_id, refund_received
        )
        assert refund_receipt.payment_receipt_id is not None
        refund_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, refund_receipt.payment_receipt_id, leftover)
        self.assertEqual(refund_parked.unapplied_cash_outcome_code.value, "accepted")
        assert refund_parked.unapplied_cash_id is not None
        refunded_at = datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        refunded = UnappliedCashRefundService(
            self.ledger, clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, refund_parked.unapplied_cash_id)
        self.assertEqual(refunded.unapplied_cash_refund_outcome_code.value, "accepted")
        assert refunded.unapplied_cash_refund_id is not None

        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_refund_journal(TENANT_ONE, refunded.unapplied_cash_refund_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.unapplied_cash_refund_id, refunded.unapplied_cash_refund_id)
        self.assertEqual(stored.invoice_draft_id, refund_draft.invoice_draft_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 2)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "unapplied_cash")
        self.assertEqual(stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "cash_receipt")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, leftover)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_refund(
                stored.tenant_account_id, refunded.unapplied_cash_refund_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_refund(stored.tenant_account_id, uuid4())
        )
        refund_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.unapplied_cash_refund_id is not None
        )
        self.assertEqual(refund_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_refunded = self.ledger.get_unapplied_cash_refund(
            refunded.unapplied_cash_refund_id
        )
        assert still_refunded is not None
        self.assertEqual(still_refunded.refund_amount, leftover)
        still_parked = self.ledger.get_unapplied_cash(refund_parked.unapplied_cash_id)
        assert still_parked is not None
        self.assertEqual(still_parked.unapplied_amount, leftover)
        self.assertEqual(still_parked.unapplied_cash_status, "parked")
        still_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert still_remaining is not None
        self.assertEqual(still_remaining.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(still_remaining.collection_case_status, "open")

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_refund_journal(TENANT_ONE, refunded.unapplied_cash_refund_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        replay_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert replay_remaining is not None
        self.assertEqual(replay_remaining.outstanding_amount, leftover_apply_remaining)

        rejected = AccountingExportService(self.ledger).propose_refund_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_refund_journal(
            TENANT_TWO, refunded.unapplied_cash_refund_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4d031c",
            source_event_key="workflow_381:step_32:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        later_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, later_draft.invoice_draft_id)
        assert later_opened.collection_case_id is not None
        later_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, later_opened.collection_case_id)
        assert later_intent.payment_intent_id is not None
        later_received = self.ledger.get_collection_case(
            later_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(later_received, leftover)
        later_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, later_intent.payment_intent_id, later_received
        )
        assert later_receipt.payment_receipt_id is not None
        later_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, later_receipt.payment_receipt_id, leftover)
        self.assertEqual(later_parked.unapplied_cash_outcome_code.value, "accepted")
        assert later_parked.unapplied_cash_id is not None
        later_refunded = UnappliedCashRefundService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 15, tzinfo=UTC)
        ).refund_unapplied_cash(TENANT_ONE, later_parked.unapplied_cash_id)
        self.assertEqual(later_refunded.unapplied_cash_refund_outcome_code.value, "accepted")
        assert later_refunded.unapplied_cash_refund_id is not None
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_refund(
                stored.tenant_account_id, later_refunded.unapplied_cash_refund_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4d041c",
            source_event_key="workflow_381:step_33:attempt_01",
            occurred_at="2026-08-16T16:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T16:00:00Z", "2026-08-16T17:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_opened.collection_case_id is not None
        crash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, crash_opened.collection_case_id)
        assert crash_intent.payment_intent_id is not None
        crash_received = self.ledger.get_collection_case(
            crash_opened.collection_case_id
        ).outstanding_amount
        crash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, crash_intent.payment_intent_id, crash_received
        )
        assert crash_receipt.payment_receipt_id is not None
        crash_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, crash_receipt.payment_receipt_id, leftover)
        assert crash_parked.unapplied_cash_id is not None
        crash_refunded = UnappliedCashRefundService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 15, tzinfo=UTC)
        ).refund_unapplied_cash(TENANT_ONE, crash_parked.unapplied_cash_id)
        self.assertEqual(crash_refunded.unapplied_cash_refund_outcome_code.value, "accepted")
        assert crash_refunded.unapplied_cash_refund_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 30, tzinfo=UTC)
        ).propose_refund_journal(TENANT_ONE, crash_refunded.unapplied_cash_refund_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_refund_journal(TENANT_ONE, crash_refunded.unapplied_cash_refund_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        healed_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert healed_remaining is not None
        self.assertEqual(healed_remaining.outstanding_amount, leftover_apply_remaining)

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    payment_receipt_id=None,
                    credit_adjustment_id=None,
                    collection_write_off_id=None,
                    unapplied_cash_id=None,
                    unapplied_cash_application_id=None,
                    unapplied_cash_refund_id=None,
                ),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    unapplied_cash_refund_id=later_refunded.unapplied_cash_refund_id,
                ),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_refund_journal(TENANT_ONE, later_refunded.unapplied_cash_refund_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(
            later_stored.unapplied_cash_refund_id,
            later_refunded.unapplied_cash_refund_id,
        )
        self.assertEqual(later_stored.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, leftover)
        self.assertEqual(
            presentment.unapplied_cash_refund_id, refunded.unapplied_cash_refund_id
        )
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.unapplied_cash_refund_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(later_presentment.proposal_lines[0].debit_amount, leftover)
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        fresh_remaining = PostgresUsageLedger(self.connection).get_collection_case(
            twenty_opened.collection_case_id
        )
        assert fresh_remaining is not None
        self.assertEqual(fresh_remaining.outstanding_amount, leftover_apply_remaining)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_refund(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_refund_journal(TENANT_ONE, refunded.unapplied_cash_refund_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE unapplied_cash_refund_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )
        raced_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert raced_remaining is not None
        self.assertEqual(raced_remaining.outstanding_amount, leftover_apply_remaining)

    def test_issued_invoice_void_journal_is_durable(self) -> None:
        """Persist one unused invoice-void journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        second_case_amount = Decimal("20.00")
        leftover_apply_remaining = Decimal("19.999")
        exclusive_amount = Decimal("100.00")
        tax_amount = Decimal("10.00")
        voided_amount = Decimal("110.00")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        self.assertNotEqual(rating.rated_total_amount, KNOWN_MORNING_TOTAL)
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None

        twenty_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e011c",
            source_event_key="workflow_381:step_40:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(twenty_usage)
        twenty_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        twenty_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, twenty_window, 1, rate_card_code="cwl_standard"
        )
        twenty_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, twenty_rating.rating_run_id
        )
        twenty_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, twenty_draft.invoice_draft_id)
        assert twenty_opened.collection_case_id is not None
        twenty_remaining = self.ledger.get_collection_case(
            twenty_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(twenty_remaining, second_case_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, twenty_opened.collection_case_id
        )
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert applied.unapplied_cash_application_id is not None
        self.assertEqual(applied.remaining_outstanding_amount, leftover_apply_remaining)
        applied_case = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(applied_case.collection_case_status, "open")

        taxed_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e021c",
            source_event_key="workflow_381:step_41:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "50000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(taxed_usage)
        taxed_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        taxed_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, taxed_window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(taxed_rating.rated_total_amount, exclusive_amount)
        taxed_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, taxed_rating.rating_run_id
        )
        tax_rate = TaxRateService(self.ledger).publish_tax_rate(TENANT_ONE, "vat", "0.1")
        self.assertEqual(tax_rate.tax_rate_outcome_code.value, "accepted")
        assessed = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, taxed_draft.invoice_draft_id, 1
        )
        self.assertEqual(assessed.tax_assessment_outcome_code.value, "accepted")
        self.assertEqual(assessed.tax_exclusive_amount, exclusive_amount)
        self.assertEqual(assessed.tax_amount, tax_amount)
        self.assertEqual(assessed.tax_inclusive_amount, voided_amount)
        issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, taxed_draft.invoice_draft_id)
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        assert issued.issued_invoice_id is not None
        self.assertEqual(issued.tax_inclusive_amount, voided_amount)
        voided = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 15, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        self.assertEqual(voided.issued_invoice_void_outcome_code.value, "accepted")
        assert voided.issued_invoice_void_id is not None
        stored_void = self.ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        assert stored_void is not None
        self.assertEqual(stored_void.voided_amount, voided_amount)

        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        invoice_journal = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, taxed_draft.invoice_draft_id)
        self.assertEqual(invoice_journal.journal_proposal_outcome_code.value, "accepted")
        assert invoice_journal.proposal_id is not None
        invoice_replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, taxed_draft.invoice_draft_id)
        self.assertEqual(invoice_replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(invoice_replay.proposal_id, invoice_journal.proposal_id)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_void_journal(TENANT_ONE, voided.issued_invoice_void_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        self.assertEqual(accepted.reversed_journal_proposal_id, invoice_journal.proposal_id)
        self.assertNotIn("journal_entry_id", accepted.as_contract_dict())
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.issued_invoice_void_id, voided.issued_invoice_void_id)
        self.assertEqual(stored.invoice_draft_id, taxed_draft.invoice_draft_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 3)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "usage_revenue")
        self.assertEqual(stored.proposal_lines[0].debit_amount, exclusive_amount)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "tax_payable")
        self.assertEqual(stored.proposal_lines[1].debit_amount, tax_amount)
        self.assertEqual(stored.proposal_lines[1].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[2].account_role_code, "accounts_receivable")
        self.assertEqual(stored.proposal_lines[2].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[2].credit_amount, voided_amount)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_issued_invoice_void(
                stored.tenant_account_id, voided.issued_invoice_void_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_issued_invoice_void(
                stored.tenant_account_id, uuid4()
            )
        )
        void_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.issued_invoice_void_id is not None
        )
        self.assertEqual(void_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_voided = self.ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        assert still_voided is not None
        self.assertEqual(still_voided.voided_amount, voided_amount)
        still_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert still_remaining is not None
        self.assertEqual(still_remaining.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(still_remaining.collection_case_status, "open")

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_void_journal(TENANT_ONE, voided.issued_invoice_void_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.reversed_journal_proposal_id, invoice_journal.proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, exclusive_amount)
        self.assertEqual(replay.proposal_lines[1].debit_amount, tax_amount)
        self.assertEqual(replay.proposal_lines[2].credit_amount, voided_amount)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        replay_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert replay_remaining is not None
        self.assertEqual(replay_remaining.outstanding_amount, leftover_apply_remaining)
        replay_void = self.ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        assert replay_void is not None
        self.assertEqual(replay_void.voided_amount, voided_amount)

        rejected = AccountingExportService(self.ledger).propose_void_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_void_journal(
            TENANT_TWO, voided.issued_invoice_void_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e031c",
            source_event_key="workflow_381:step_42:attempt_01",
            occurred_at="2026-08-16T18:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T18:00:00Z", "2026-08-16T19:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        later_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, later_draft.invoice_draft_id)
        self.assertEqual(later_issued.issued_invoice_outcome_code.value, "accepted")
        assert later_issued.issued_invoice_id is not None
        later_voided = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 20, 15, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, later_issued.issued_invoice_id)
        self.assertEqual(later_voided.issued_invoice_void_outcome_code.value, "accepted")
        assert later_voided.issued_invoice_void_id is not None
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_issued_invoice_void(
                stored.tenant_account_id, later_voided.issued_invoice_void_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e041c",
            source_event_key="workflow_381:step_43:attempt_01",
            occurred_at="2026-08-16T19:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T19:00:00Z", "2026-08-16T20:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_issued.issued_invoice_id is not None
        crash_voided = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 15, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, crash_issued.issued_invoice_id)
        self.assertEqual(crash_voided.issued_invoice_void_outcome_code.value, "accepted")
        assert crash_voided.issued_invoice_void_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 30, tzinfo=UTC)
        ).propose_void_journal(TENANT_ONE, crash_voided.issued_invoice_void_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_void_journal(TENANT_ONE, crash_voided.issued_invoice_void_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        healed_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert healed_remaining is not None
        self.assertEqual(healed_remaining.outstanding_amount, leftover_apply_remaining)
        healed_void = self.ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        assert healed_void is not None
        self.assertEqual(healed_void.voided_amount, voided_amount)

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    payment_receipt_id=None,
                    credit_adjustment_id=None,
                    collection_write_off_id=None,
                    unapplied_cash_id=None,
                    unapplied_cash_application_id=None,
                    unapplied_cash_refund_id=None,
                    issued_invoice_void_id=None,
                ),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    issued_invoice_void_id=later_voided.issued_invoice_void_id,
                ),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_void_journal(TENANT_ONE, later_voided.issued_invoice_void_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(
            later_stored.issued_invoice_void_id,
            later_voided.issued_invoice_void_id,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, exclusive_amount)
        self.assertEqual(presentment.proposal_lines[1].debit_amount, tax_amount)
        self.assertEqual(presentment.proposal_lines[2].credit_amount, voided_amount)
        self.assertEqual(presentment.issued_invoice_void_id, voided.issued_invoice_void_id)
        self.assertNotIn("journal_entry_id", presentment.as_contract_dict())
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.issued_invoice_void_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(
            later_presentment.issued_invoice_void_id, later_voided.issued_invoice_void_id
        )
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        fresh_remaining = PostgresUsageLedger(self.connection).get_collection_case(
            twenty_opened.collection_case_id
        )
        assert fresh_remaining is not None
        self.assertEqual(fresh_remaining.outstanding_amount, leftover_apply_remaining)
        fresh_void = PostgresUsageLedger(self.connection).get_issued_invoice_void(
            voided.issued_invoice_void_id
        )
        assert fresh_void is not None
        self.assertEqual(fresh_void.voided_amount, voided_amount)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_issued_invoice_void(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_void_journal(TENANT_ONE, voided.issued_invoice_void_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(raced.reversed_journal_proposal_id, invoice_journal.proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_invoice_void_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )
        raced_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert raced_remaining is not None
        self.assertEqual(raced_remaining.outstanding_amount, leftover_apply_remaining)
        raced_void = self.ledger.get_issued_invoice_void(voided.issued_invoice_void_id)
        assert raced_void is not None
        self.assertEqual(raced_void.voided_amount, voided_amount)

    def test_issued_credit_note_void_journal_is_durable(self) -> None:
        """Persist one unused credit-note-void journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        second_case_amount = Decimal("20.00")
        leftover_apply_remaining = Decimal("19.999")
        exclusive_amount = Decimal("100.00")
        invoice_voided_amount = Decimal("110.00")
        credit_exclusive = Decimal("10.00")
        credit_tax = Decimal("1.00")
        credit_voided_amount = Decimal("11.00")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        self.assertNotEqual(rating.rated_total_amount, KNOWN_MORNING_TOTAL)
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None

        twenty_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e051c",
            source_event_key="workflow_381:step_50:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(twenty_usage)
        twenty_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        twenty_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, twenty_window, 1, rate_card_code="cwl_standard"
        )
        twenty_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, twenty_rating.rating_run_id
        )
        twenty_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, twenty_draft.invoice_draft_id)
        assert twenty_opened.collection_case_id is not None
        twenty_remaining = self.ledger.get_collection_case(
            twenty_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(twenty_remaining, second_case_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, twenty_opened.collection_case_id
        )
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert applied.unapplied_cash_application_id is not None
        self.assertEqual(applied.remaining_outstanding_amount, leftover_apply_remaining)
        applied_case = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(applied_case.collection_case_status, "open")

        tax_rate = TaxRateService(self.ledger).publish_tax_rate(TENANT_ONE, "vat", "0.1")
        self.assertEqual(tax_rate.tax_rate_outcome_code.value, "accepted")
        invoice_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e061c",
            source_event_key="workflow_381:step_51:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "50000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(invoice_usage)
        invoice_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        invoice_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, invoice_window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(invoice_rating.rated_total_amount, exclusive_amount)
        invoice_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, invoice_rating.rating_run_id
        )
        invoice_assessed = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, invoice_draft.invoice_draft_id, 1
        )
        self.assertEqual(invoice_assessed.tax_assessment_outcome_code.value, "accepted")
        self.assertEqual(invoice_assessed.tax_inclusive_amount, invoice_voided_amount)
        issued_invoice = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, invoice_draft.invoice_draft_id)
        self.assertEqual(issued_invoice.issued_invoice_outcome_code.value, "accepted")
        assert issued_invoice.issued_invoice_id is not None
        invoice_voided = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 15, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, issued_invoice.issued_invoice_id)
        self.assertEqual(invoice_voided.issued_invoice_void_outcome_code.value, "accepted")
        assert invoice_voided.issued_invoice_void_id is not None
        stored_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert stored_invoice_void is not None
        self.assertEqual(stored_invoice_void.voided_amount, invoice_voided_amount)

        credit_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e071c",
            source_event_key="workflow_381:step_52:attempt_01",
            occurred_at="2026-08-16T13:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "50000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(credit_usage)
        credit_window = TimeWindow.from_iso8601(
            "2026-08-16T13:00:00Z", "2026-08-16T14:00:00Z"
        )
        credit_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, credit_window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(credit_rating.rated_total_amount, exclusive_amount)
        credit_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, credit_rating.rating_run_id
        )
        credit_assessed = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, credit_draft.invoice_draft_id, 1
        )
        self.assertEqual(credit_assessed.tax_assessment_outcome_code.value, "accepted")
        self.assertEqual(credit_assessed.tax_inclusive_amount, invoice_voided_amount)
        credit = CreditAdjustmentService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).record_credit_adjustment(
            TENANT_ONE, credit_draft.invoice_draft_id, credit_voided_amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        assert credit.proposal_id is not None
        issued_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(issued_note.issued_credit_note_outcome_code.value, "accepted")
        assert issued_note.issued_credit_note_id is not None
        voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, issued_note.issued_credit_note_id)
        self.assertEqual(voided.issued_credit_note_void_outcome_code.value, "accepted")
        assert voided.issued_credit_note_void_id is not None
        stored_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert stored_void is not None
        self.assertEqual(stored_void.voided_amount, credit_voided_amount)

        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        credit_journal = self.ledger.find_journal_proposal_for_credit_adjustment(
            tenant.tenant_account_id, credit.credit_adjustment_id
        )
        self.assertIsNotNone(credit_journal)
        assert credit_journal is not None
        self.assertEqual(credit_journal.journal_proposal_id, credit.proposal_id)
        credit_replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_credit_journal(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(credit_replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(credit_replay.proposal_id, credit.proposal_id)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_credit_note_void_journal(TENANT_ONE, voided.issued_credit_note_void_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        self.assertEqual(accepted.reversed_journal_proposal_id, credit.proposal_id)
        self.assertNotIn("journal_entry_id", accepted.as_contract_dict())
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.issued_credit_note_void_id, voided.issued_credit_note_void_id)
        self.assertEqual(stored.invoice_draft_id, credit_draft.invoice_draft_id)
        self.assertIsNone(stored.issued_invoice_void_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 3)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(stored.proposal_lines[0].debit_amount, credit_voided_amount)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "usage_revenue")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, credit_exclusive)
        self.assertEqual(stored.proposal_lines[2].account_role_code, "tax_payable")
        self.assertEqual(stored.proposal_lines[2].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[2].credit_amount, credit_tax)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal_for_issued_credit_note_void(
                stored.tenant_account_id, voided.issued_credit_note_void_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_issued_credit_note_void(
                stored.tenant_account_id, uuid4()
            )
        )
        void_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.issued_credit_note_void_id is not None
        )
        self.assertEqual(void_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_voided = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert still_voided is not None
        self.assertEqual(still_voided.voided_amount, credit_voided_amount)
        still_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert still_invoice_void is not None
        self.assertEqual(still_invoice_void.voided_amount, invoice_voided_amount)
        still_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert still_remaining is not None
        self.assertEqual(still_remaining.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(still_remaining.collection_case_status, "open")

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_credit_note_void_journal(TENANT_ONE, voided.issued_credit_note_void_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.reversed_journal_proposal_id, credit.proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, credit_voided_amount)
        self.assertEqual(replay.proposal_lines[1].credit_amount, credit_exclusive)
        self.assertEqual(replay.proposal_lines[2].credit_amount, credit_tax)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        replay_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert replay_remaining is not None
        self.assertEqual(replay_remaining.outstanding_amount, leftover_apply_remaining)
        replay_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert replay_void is not None
        self.assertEqual(replay_void.voided_amount, credit_voided_amount)
        replay_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert replay_invoice_void is not None
        self.assertEqual(replay_invoice_void.voided_amount, invoice_voided_amount)

        rejected = AccountingExportService(self.ledger).propose_credit_note_void_journal(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_credit_note_void_journal(
            TENANT_TWO, voided.issued_credit_note_void_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e081c",
            source_event_key="workflow_381:step_53:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        later_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
        ).record_credit_adjustment(
            TENANT_ONE, later_draft.invoice_draft_id, leftover, "billing_error"
        )
        self.assertEqual(later_credit.credit_adjustment_outcome_code.value, "accepted")
        assert later_credit.credit_adjustment_id is not None
        later_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 20, 15, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, later_credit.credit_adjustment_id)
        self.assertEqual(later_note.issued_credit_note_outcome_code.value, "accepted")
        assert later_note.issued_credit_note_id is not None
        later_voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 20, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, later_note.issued_credit_note_id)
        self.assertEqual(later_voided.issued_credit_note_void_outcome_code.value, "accepted")
        assert later_voided.issued_credit_note_void_id is not None
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_issued_credit_note_void(
                stored.tenant_account_id, later_voided.issued_credit_note_void_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e091c",
            source_event_key="workflow_381:step_54:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).record_credit_adjustment(
            TENANT_ONE, crash_draft.invoice_draft_id, leftover, "goodwill"
        )
        assert crash_credit.credit_adjustment_id is not None
        crash_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 15, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, crash_credit.credit_adjustment_id)
        assert crash_note.issued_credit_note_id is not None
        crash_voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, crash_note.issued_credit_note_id)
        self.assertEqual(crash_voided.issued_credit_note_void_outcome_code.value, "accepted")
        assert crash_voided.issued_credit_note_void_id is not None
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 45, tzinfo=UTC)
        ).propose_credit_note_void_journal(TENANT_ONE, crash_voided.issued_credit_note_void_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_credit_note_void_journal(TENANT_ONE, crash_voided.issued_credit_note_void_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        healed_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert healed_remaining is not None
        self.assertEqual(healed_remaining.outstanding_amount, leftover_apply_remaining)
        healed_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert healed_void is not None
        self.assertEqual(healed_void.voided_amount, credit_voided_amount)
        healed_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert healed_invoice_void is not None
        self.assertEqual(healed_invoice_void.voided_amount, invoice_voided_amount)

        missing_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0a1c",
            source_event_key="workflow_381:step_55:attempt_01",
            occurred_at="2026-08-16T20:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(missing_usage)
        missing_window = TimeWindow.from_iso8601(
            "2026-08-16T20:00:00Z", "2026-08-16T21:00:00Z"
        )
        missing_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, missing_window, 1, rate_card_code="cwl_standard"
        )
        missing_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, missing_rating.rating_run_id
        )
        missing_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
        ).record_credit_adjustment(
            TENANT_ONE, missing_draft.invoice_draft_id, leftover, "goodwill"
        )
        assert missing_credit.credit_adjustment_id is not None
        assert missing_credit.proposal_id is not None
        missing_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 15, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, missing_credit.credit_adjustment_id)
        assert missing_note.issued_credit_note_id is not None
        missing_voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 22, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, missing_note.issued_credit_note_id)
        self.assertEqual(missing_voided.issued_credit_note_void_outcome_code.value, "accepted")
        assert missing_voided.issued_credit_note_void_id is not None
        self.connection.execute(
            "DELETE FROM billing_core.journal_proposal_line "
            "WHERE journal_proposal_id = %s",
            (missing_credit.proposal_id,),
        )
        self.connection.execute(
            "DELETE FROM billing_core.journal_proposal "
            "WHERE journal_proposal_id = %s",
            (missing_credit.proposal_id,),
        )
        self.connection.commit()
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_credit_adjustment(
                tenant.tenant_account_id, missing_credit.credit_adjustment_id
            )
        )
        missing_original = AccountingExportService(self.ledger).propose_credit_note_void_journal(
            TENANT_ONE, missing_voided.issued_credit_note_void_id
        )
        self.assertEqual(missing_original.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            missing_original.rejection_reason_code.value,
            JournalProposalRejectionReasonCode.CREDIT_JOURNAL_NOT_FOUND.value,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            2,
        )

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    payment_receipt_id=None,
                    credit_adjustment_id=None,
                    collection_write_off_id=None,
                    unapplied_cash_id=None,
                    unapplied_cash_application_id=None,
                    unapplied_cash_refund_id=None,
                    issued_invoice_void_id=None,
                    issued_credit_note_void_id=None,
                ),
                stored.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(
                    stored,
                    issued_credit_note_void_id=later_voided.issued_credit_note_void_id,
                ),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_credit_note_void_journal(TENANT_ONE, later_voided.issued_credit_note_void_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(
            later_stored.issued_credit_note_void_id,
            later_voided.issued_credit_note_void_id,
        )
        self.assertEqual(later.reversed_journal_proposal_id, later_credit.proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, credit_voided_amount)
        self.assertEqual(presentment.proposal_lines[1].credit_amount, credit_exclusive)
        self.assertEqual(presentment.proposal_lines[2].credit_amount, credit_tax)
        self.assertEqual(presentment.issued_credit_note_void_id, voided.issued_credit_note_void_id)
        self.assertNotIn("journal_entry_id", presentment.as_contract_dict())
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.issued_credit_note_void_id is not None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(
            later_presentment.issued_credit_note_void_id,
            later_voided.issued_credit_note_void_id,
        )
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        fresh_remaining = PostgresUsageLedger(self.connection).get_collection_case(
            twenty_opened.collection_case_id
        )
        assert fresh_remaining is not None
        self.assertEqual(fresh_remaining.outstanding_amount, leftover_apply_remaining)
        fresh_void = PostgresUsageLedger(self.connection).get_issued_credit_note_void(
            voided.issued_credit_note_void_id
        )
        assert fresh_void is not None
        self.assertEqual(fresh_void.voided_amount, credit_voided_amount)
        fresh_invoice_void = PostgresUsageLedger(self.connection).get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert fresh_invoice_void is not None
        self.assertEqual(fresh_invoice_void.voided_amount, invoice_voided_amount)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal_for_issued_credit_note_void(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_credit_note_void_journal(TENANT_ONE, voided.issued_credit_note_void_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(raced.reversed_journal_proposal_id, credit.proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                "WHERE issued_credit_note_void_id IS NOT NULL"
            ).fetchone()[0],
            3,
        )
        raced_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert raced_remaining is not None
        self.assertEqual(raced_remaining.outstanding_amount, leftover_apply_remaining)
        raced_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert raced_void is not None
        self.assertEqual(raced_void.voided_amount, credit_voided_amount)
        raced_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert raced_invoice_void is not None
        self.assertEqual(raced_invoice_void.voided_amount, invoice_voided_amount)

    def test_invoice_draft_journal_is_durable(self) -> None:
        """Persist one invoice-draft journal and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        cash_journal_debit = Decimal("0.003705")
        second_case_amount = Decimal("20.00")
        leftover_apply_remaining = Decimal("19.999")
        exclusive_amount = Decimal("100.00")
        tax_amount = Decimal("10.00")
        invoice_voided_amount = Decimal("110.00")
        credit_voided_amount = Decimal("11.00")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        self.assertNotEqual(rating.rated_total_amount, KNOWN_MORNING_TOTAL)
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        opened = CollectionCaseService(self.ledger, clock=lambda: CATALOG_START).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(opened.collection_case_outcome_code.value, "accepted")
        assert opened.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, opened.collection_case_id)
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(opened.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None

        twenty_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0b1c",
            source_event_key="workflow_381:step_60:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(twenty_usage)
        twenty_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        twenty_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, twenty_window, 1, rate_card_code="cwl_standard"
        )
        twenty_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, twenty_rating.rating_run_id
        )
        twenty_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, twenty_draft.invoice_draft_id)
        assert twenty_opened.collection_case_id is not None
        twenty_remaining = self.ledger.get_collection_case(
            twenty_opened.collection_case_id
        ).outstanding_amount
        self.assertEqual(twenty_remaining, second_case_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        applied = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, twenty_opened.collection_case_id
        )
        self.assertEqual(applied.unapplied_cash_application_outcome_code.value, "accepted")
        assert applied.unapplied_cash_application_id is not None
        self.assertEqual(applied.remaining_outstanding_amount, leftover_apply_remaining)
        applied_case = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(applied_case.collection_case_status, "open")

        cash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0a2c",
            source_event_key="workflow_381:step_59:attempt_01",
            occurred_at="2026-08-16T12:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(cash_usage)
        cash_window = TimeWindow.from_iso8601(
            "2026-08-16T12:00:00Z", "2026-08-16T13:00:00Z"
        )
        cash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, cash_window, 1, rate_card_code="cwl_standard"
        )
        cash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, cash_rating.rating_run_id
        )
        cash_opened = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, cash_draft.invoice_draft_id)
        assert cash_opened.collection_case_id is not None
        cash_outstanding = self.ledger.get_collection_case(
            cash_opened.collection_case_id
        ).outstanding_amount
        self.assertGreater(cash_outstanding, cash_journal_debit)
        cash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, cash_opened.collection_case_id)
        assert cash_intent.payment_intent_id is not None
        cash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(
            TENANT_ONE, cash_intent.payment_intent_id, cash_journal_debit
        )
        self.assertEqual(cash_receipt.payment_settlement_outcome_code.value, "accepted")
        assert cash_receipt.payment_receipt_id is not None
        cash_replay = AccountingExportService(self.ledger).propose_cash_journal(
            TENANT_ONE, cash_receipt.payment_receipt_id
        )
        self.assertEqual(cash_replay.journal_proposal_outcome_code.value, "duplicate_replay")
        assert cash_replay.proposal_id is not None
        cash_journal = self.ledger.get_journal_proposal(cash_replay.proposal_id)
        assert cash_journal is not None
        self.assertEqual(cash_journal.proposal_lines[0].debit_amount, cash_journal_debit)

        tax_rate = TaxRateService(self.ledger).publish_tax_rate(TENANT_ONE, "vat", "0.1")
        self.assertEqual(tax_rate.tax_rate_outcome_code.value, "accepted")
        invoice_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0c1c",
            source_event_key="workflow_381:step_61:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "50000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(invoice_usage)
        invoice_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        invoice_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, invoice_window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(invoice_rating.rated_total_amount, exclusive_amount)
        invoice_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, invoice_rating.rating_run_id
        )
        invoice_assessed = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, invoice_draft.invoice_draft_id, 1
        )
        self.assertEqual(invoice_assessed.tax_assessment_outcome_code.value, "accepted")
        self.assertEqual(invoice_assessed.tax_inclusive_amount, invoice_voided_amount)
        issued_invoice = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, invoice_draft.invoice_draft_id)
        self.assertEqual(issued_invoice.issued_invoice_outcome_code.value, "accepted")
        assert issued_invoice.issued_invoice_id is not None
        invoice_voided = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 15, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, issued_invoice.issued_invoice_id)
        self.assertEqual(invoice_voided.issued_invoice_void_outcome_code.value, "accepted")
        assert invoice_voided.issued_invoice_void_id is not None
        stored_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert stored_invoice_void is not None
        self.assertEqual(stored_invoice_void.voided_amount, invoice_voided_amount)

        credit_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0d1c",
            source_event_key="workflow_381:step_62:attempt_01",
            occurred_at="2026-08-16T13:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "50000000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(credit_usage)
        credit_window = TimeWindow.from_iso8601(
            "2026-08-16T13:00:00Z", "2026-08-16T14:00:00Z"
        )
        credit_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, credit_window, 1, rate_card_code="cwl_standard"
        )
        self.assertEqual(credit_rating.rated_total_amount, exclusive_amount)
        credit_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, credit_rating.rating_run_id
        )
        credit_assessed = TaxAssessmentService(self.ledger).assess_tax(
            TENANT_ONE, credit_draft.invoice_draft_id, 1
        )
        self.assertEqual(credit_assessed.tax_assessment_outcome_code.value, "accepted")
        self.assertEqual(credit_assessed.tax_inclusive_amount, invoice_voided_amount)
        credit = CreditAdjustmentService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).record_credit_adjustment(
            TENANT_ONE, credit_draft.invoice_draft_id, credit_voided_amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        issued_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(issued_note.issued_credit_note_outcome_code.value, "accepted")
        assert issued_note.issued_credit_note_id is not None
        voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, issued_note.issued_credit_note_id)
        self.assertEqual(voided.issued_credit_note_void_outcome_code.value, "accepted")
        assert voided.issued_credit_note_void_id is not None
        stored_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert stored_void is not None
        self.assertEqual(stored_void.voided_amount, credit_voided_amount)

        tenant = self.ledger.require_tenant(TENANT_ONE)
        proposed_at = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        accepted = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, invoice_draft.invoice_draft_id)
        self.assertEqual(accepted.journal_proposal_outcome_code.value, "accepted")
        assert accepted.proposal_id is not None
        stored = self.ledger.get_journal_proposal(accepted.proposal_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.invoice_draft_id, invoice_draft.invoice_draft_id)
        self.assertIsNone(stored.payment_receipt_id)
        self.assertIsNone(stored.credit_adjustment_id)
        self.assertIsNone(stored.collection_write_off_id)
        self.assertIsNone(stored.unapplied_cash_id)
        self.assertIsNone(stored.unapplied_cash_application_id)
        self.assertIsNone(stored.unapplied_cash_refund_id)
        self.assertIsNone(stored.issued_invoice_void_id)
        self.assertIsNone(stored.issued_credit_note_void_id)
        self.assertEqual(stored.proposal_status, "validated")
        self.assertNotEqual(stored.proposal_status, "posted")
        self.assertEqual(stored.transaction_currency, "USD")
        self.assertEqual(len(stored.proposal_lines), 3)
        self.assertEqual(stored.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(stored.proposal_lines[0].debit_amount, invoice_voided_amount)
        self.assertEqual(stored.proposal_lines[0].credit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].account_role_code, "usage_revenue")
        self.assertEqual(stored.proposal_lines[1].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[1].credit_amount, exclusive_amount)
        self.assertEqual(stored.proposal_lines[2].account_role_code, "tax_payable")
        self.assertEqual(stored.proposal_lines[2].debit_amount, Decimal("0"))
        self.assertEqual(stored.proposal_lines[2].credit_amount, tax_amount)
        self.assertIsInstance(stored.proposal_lines[0].debit_amount, Decimal)
        self.assertNotIsInstance(stored.proposal_lines[0].debit_amount, float)
        self.assertEqual(
            self.ledger.find_journal_proposal(
                stored.tenant_account_id,
                stored.invoice_draft_id,
                stored.source_payload_hash,
                stored.proposal_contract_version,
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.find_journal_proposal_for_invoice_draft(
                stored.tenant_account_id, invoice_draft.invoice_draft_id
            ),
            stored,
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_invoice_draft(
                stored.tenant_account_id, uuid4()
            )
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal(
                stored.tenant_account_id,
                cash_journal.invoice_draft_id,
                cash_journal.source_payload_hash,
                cash_journal.proposal_contract_version,
            )
        )
        draft_rows = tuple(
            proposal
            for proposal in self.ledger.list_journal_proposals(stored.tenant_account_id)
            if proposal.payment_receipt_id is None
            and proposal.credit_adjustment_id is None
            and proposal.collection_write_off_id is None
            and proposal.unapplied_cash_refund_id is None
            and proposal.unapplied_cash_id is None
            and proposal.unapplied_cash_application_id is None
            and proposal.issued_invoice_void_id is None
            and proposal.issued_credit_note_void_id is None
        )
        self.assertEqual(draft_rows, (stored,))
        self.assertEqual(
            self.ledger.list_journal_proposals(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_journal_proposal(uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            and event.source_id == stored.journal_proposal_id
        ]
        self.assertEqual(len(outbox_rows), 1)
        still_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert still_remaining is not None
        self.assertEqual(still_remaining.outstanding_amount, leftover_apply_remaining)
        self.assertEqual(still_remaining.collection_case_status, "open")
        still_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert still_invoice_void is not None
        self.assertEqual(still_invoice_void.voided_amount, invoice_voided_amount)
        still_voided = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert still_voided is not None
        self.assertEqual(still_voided.voided_amount, credit_voided_amount)
        still_cash = self.ledger.get_journal_proposal(cash_journal.journal_proposal_id)
        assert still_cash is not None
        self.assertEqual(still_cash.proposal_lines[0].debit_amount, cash_journal_debit)

        replay = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, invoice_draft.invoice_draft_id)
        self.assertEqual(replay.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.proposal_id, stored.journal_proposal_id)
        self.assertEqual(replay.proposal_lines[0].debit_amount, invoice_voided_amount)
        self.assertEqual(replay.proposal_lines[1].credit_amount, exclusive_amount)
        self.assertEqual(replay.proposal_lines[2].credit_amount, tax_amount)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                    and event.source_id == stored.journal_proposal_id
                ]
            ),
            1,
        )
        replay_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert replay_remaining is not None
        self.assertEqual(replay_remaining.outstanding_amount, leftover_apply_remaining)
        replay_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert replay_invoice_void is not None
        self.assertEqual(replay_invoice_void.voided_amount, invoice_voided_amount)
        replay_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert replay_void is not None
        self.assertEqual(replay_void.voided_amount, credit_voided_amount)

        rejected = AccountingExportService(self.ledger).propose_journal(TENANT_ONE, uuid4())
        self.assertEqual(rejected.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            1,
        )
        mismatch = AccountingExportService(self.ledger).propose_journal(
            TENANT_TWO, invoice_draft.invoice_draft_id
        )
        self.assertEqual(mismatch.journal_proposal_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            1,
        )

        later_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0e1c",
            source_event_key="workflow_381:step_63:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(later_usage)
        later_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        later_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, later_window, 1, rate_card_code="cwl_standard"
        )
        later_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, later_rating.rating_run_id
        )
        self.assertIsNone(
            self.ledger.find_journal_proposal_for_invoice_draft(
                stored.tenant_account_id, later_draft.invoice_draft_id
            )
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4e0f1c",
            source_event_key="workflow_381:step_64:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_composed = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 45, tzinfo=UTC)
        ).propose_journal(TENANT_ONE, crash_draft.invoice_draft_id)
        self.assertEqual(crash_composed.journal_proposal_outcome_code.value, "accepted")
        assert crash_composed.proposal_id is not None
        crash_stored = self.ledger.get_journal_proposal(crash_composed.proposal_id)
        assert crash_stored is not None
        self.connection.execute(
            "DELETE FROM billing_core.webhook_outbox_event "
            "WHERE event_type_code = %s AND source_id = %s",
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, crash_stored.journal_proposal_id),
        )
        self.connection.commit()
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
            ]
        )
        healed = AccountingExportService(
            self.ledger, clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, crash_draft.invoice_draft_id)
        self.assertEqual(healed.journal_proposal_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.proposal_id, crash_stored.journal_proposal_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            2,
        )
        healed_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert healed_remaining is not None
        self.assertEqual(healed_remaining.outstanding_amount, leftover_apply_remaining)
        healed_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert healed_invoice_void is not None
        self.assertEqual(healed_invoice_void.voided_amount, invoice_voided_amount)
        healed_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert healed_void is not None
        self.assertEqual(healed_void.voided_amount, credit_voided_amount)
        healed_cash = self.ledger.get_journal_proposal(cash_journal.journal_proposal_id)
        assert healed_cash is not None
        self.assertEqual(healed_cash.proposal_lines[0].debit_amount, cash_journal_debit)

        self.assertEqual(self.ledger.insert_journal_proposal(stored, stored.proposal_lines), stored)
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(cash_journal, payment_receipt_id=None),
                cash_journal.proposal_lines,
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_journal_proposal(
                replace(stored, invoice_draft_id=later_draft.invoice_draft_id),
                stored.proposal_lines,
            )
        later = AccountingExportService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).propose_journal(TENANT_ONE, later_draft.invoice_draft_id)
        self.assertEqual(later.journal_proposal_outcome_code.value, "accepted")
        assert later.proposal_id is not None
        later_stored = self.ledger.get_journal_proposal(later.proposal_id)
        assert later_stored is not None
        self.assertEqual(later_stored.invoice_draft_id, later_draft.invoice_draft_id)
        self.assertIsNone(later_stored.issued_invoice_void_id)
        self.assertEqual(len(later_stored.proposal_lines), 2)
        self.assertEqual(later_stored.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(later_stored.proposal_lines[1].account_role_code, "usage_revenue")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_journal_proposal(stored.journal_proposal_id)
        self.assertEqual(reloaded, stored)
        presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, stored.journal_proposal_id
        )
        self.assertEqual(presentment.proposal_id, stored.journal_proposal_id)
        self.assertEqual(presentment.proposal_status, "validated")
        self.assertEqual(presentment.proposal_lines[0].debit_amount, invoice_voided_amount)
        self.assertEqual(presentment.proposal_lines[1].credit_amount, exclusive_amount)
        self.assertEqual(presentment.proposal_lines[2].credit_amount, tax_amount)
        self.assertEqual(presentment.invoice_draft_id, invoice_draft.invoice_draft_id)
        self.assertNotIn("journal_entry_id", presentment.as_contract_dict())
        page = AccountingExportService(fresh).list_journal_proposals(TENANT_ONE)
        self.assertEqual(
            {
                row.proposal_id
                for row in page.journal_proposals
                if row.payment_receipt_id is None
                and row.credit_adjustment_id is None
                and row.collection_write_off_id is None
                and row.unapplied_cash_refund_id is None
                and row.unapplied_cash_id is None
                and row.unapplied_cash_application_id is None
                and row.issued_invoice_void_id is None
                and row.issued_credit_note_void_id is None
            },
            {
                stored.journal_proposal_id,
                later.proposal_id,
                crash_stored.journal_proposal_id,
            },
        )
        later_presentment = AccountingExportService(fresh).get_journal_proposal(
            TENANT_ONE, later.proposal_id
        )
        self.assertEqual(later_presentment.invoice_draft_id, later_draft.invoice_draft_id)
        reloaded_presentment = AccountingExportService(
            PostgresUsageLedger(self.connection)
        ).get_journal_proposal(TENANT_ONE, stored.journal_proposal_id)
        self.assertEqual(reloaded_presentment.proposal_id, presentment.proposal_id)
        fresh_remaining = PostgresUsageLedger(self.connection).get_collection_case(
            twenty_opened.collection_case_id
        )
        assert fresh_remaining is not None
        self.assertEqual(fresh_remaining.outstanding_amount, leftover_apply_remaining)
        fresh_invoice_void = PostgresUsageLedger(self.connection).get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert fresh_invoice_void is not None
        self.assertEqual(fresh_invoice_void.voided_amount, invoice_voided_amount)
        fresh_void = PostgresUsageLedger(self.connection).get_issued_credit_note_void(
            voided.issued_credit_note_void_id
        )
        assert fresh_void is not None
        self.assertEqual(fresh_void.voided_amount, credit_voided_amount)
        fresh_cash = PostgresUsageLedger(self.connection).get_journal_proposal(
            cash_journal.journal_proposal_id
        )
        assert fresh_cash is not None
        self.assertEqual(fresh_cash.proposal_lines[0].debit_amount, cash_journal_debit)
        with self.assertRaises(JournalProposalQueryError) as missing_pin:
            AccountingExportService(fresh).get_journal_proposal("", stored.journal_proposal_id)
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(JournalProposalQueryError) as other_pin:
            AccountingExportService(fresh).get_journal_proposal(
                TENANT_TWO, stored.journal_proposal_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "proposal_not_found")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the repository insert path used after a concurrent identity race."""

            def find_journal_proposal(self, *args, **kwargs):
                return None

        raced = AccountingExportService(
            BlindFindLedger(self.connection), clock=lambda: proposed_at
        ).propose_journal(TENANT_ONE, invoice_draft.invoice_draft_id)
        self.assertEqual(raced.journal_proposal_outcome_code.value, "accepted")
        self.assertEqual(raced.proposal_id, stored.journal_proposal_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.journal_proposal "
                f"WHERE {DRAFT_ONLY_JOURNAL_WHERE}"
            ).fetchone()[0],
            3,
        )
        raced_remaining = self.ledger.get_collection_case(twenty_opened.collection_case_id)
        assert raced_remaining is not None
        self.assertEqual(raced_remaining.outstanding_amount, leftover_apply_remaining)
        raced_invoice_void = self.ledger.get_issued_invoice_void(
            invoice_voided.issued_invoice_void_id
        )
        assert raced_invoice_void is not None
        self.assertEqual(raced_invoice_void.voided_amount, invoice_voided_amount)
        raced_void = self.ledger.get_issued_credit_note_void(voided.issued_credit_note_void_id)
        assert raced_void is not None
        self.assertEqual(raced_void.voided_amount, credit_voided_amount)
        raced_cash = self.ledger.get_journal_proposal(cash_journal.journal_proposal_id)
        assert raced_cash is not None
        self.assertEqual(raced_cash.proposal_lines[0].debit_amount, cash_journal_debit)

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
        at_budget = SpendBudgetService(fresh, clock=lambda: published_at).publish_spend_budget(
            TENANT_ONE,
            account.billing_account_id,
            "USD",
            evaluation.rated_amount,
            MORNING_WINDOW,
        )
        self.assertEqual(at_budget.spend_budget_outcome_code.value, "accepted")
        assert at_budget.spend_budget_id is not None
        first_approaching = SpendBudgetApproachingSignalService(
            fresh, clock=lambda: published_at
        ).observe_spend_budget_approaching(TENANT_ONE, at_budget.spend_budget_id)
        self.assertEqual(
            first_approaching.spend_budget_approaching_signal_outcome_code.value, "accepted"
        )
        self.assertEqual(first_approaching.utilization_status, "at")
        approaching_events = [
            event
            for event in fresh.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)
            if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_APPROACHING
        ]
        self.assertEqual(len(approaching_events), 1)
        self.assertEqual(approaching_events[0].source_id, at_budget.spend_budget_id)
        replay_approaching = SpendBudgetApproachingSignalService(
            fresh, clock=lambda: published_at
        ).observe_spend_budget_approaching(TENANT_ONE, at_budget.spend_budget_id)
        self.assertEqual(
            replay_approaching.spend_budget_approaching_signal_outcome_code.value,
            "duplicate_replay",
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in fresh.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)
                    if event.event_type_code == EVENT_TYPE_SPEND_BUDGET_APPROACHING
                ]
            ),
            1,
        )
        reloaded_approaching = SpendBudgetApproachingSignalService(
            PostgresUsageLedger(self.connection), clock=lambda: published_at
        ).observe_spend_budget_approaching(TENANT_ONE, at_budget.spend_budget_id)
        self.assertEqual(
            reloaded_approaching.spend_budget_approaching_signal_outcome_code.value,
            "duplicate_replay",
        )
        approaching_observation = SpendBudgetApproachingSignalPresentmentService(
            fresh
        ).present_spend_budget_approaching_signal(TENANT_ONE, at_budget.spend_budget_id)
        self.assertEqual(approaching_observation.approaching_signal.utilization_status, "at")
        self.assertEqual(len(approaching_observation.webhook_outbox_events), 1)
        self.assertEqual(
            approaching_observation.webhook_outbox_events[0].event_type_code,
            EVENT_TYPE_SPEND_BUDGET_APPROACHING,
        )
        self.assertEqual(
            approaching_observation.webhook_outbox_events[0].source_id,
            at_budget.spend_budget_id,
        )
        reloaded_approaching_observation = SpendBudgetApproachingSignalPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_spend_budget_approaching_signal(TENANT_ONE, at_budget.spend_budget_id)
        self.assertEqual(len(reloaded_approaching_observation.webhook_outbox_events), 1)
        self.assertEqual(
            reloaded_approaching_observation.webhook_outbox_events[0].outbox_event_id,
            approaching_observation.webhook_outbox_events[0].outbox_event_id,
        )
        under_approaching_observation = SpendBudgetApproachingSignalPresentmentService(
            fresh
        ).present_spend_budget_approaching_signal(TENANT_ONE, stored.spend_budget_id)
        self.assertEqual(
            under_approaching_observation.approaching_signal.utilization_status, "under"
        )
        self.assertEqual(under_approaching_observation.webhook_outbox_events, ())
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

    def test_issued_credit_note_is_durable(self) -> None:
        """Persist one issued_credit_note and keep GET presentment after restart."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        stored_draft = self.ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        self.assertGreater(stored_draft.drafted_total_amount, Decimal("0.00175"))
        first_credit_amount = Decimal("0.001")
        later_credit_amount = Decimal("0.0005")
        crash_credit_amount = Decimal("0.00025")
        credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, first_credit_amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        issued_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        accepted = IssuedCreditNoteService(
            self.ledger, clock=lambda: issued_at
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(accepted.issued_credit_note_outcome_code.value, "accepted")
        assert accepted.issued_credit_note_id is not None
        stored = self.ledger.get_issued_credit_note(accepted.issued_credit_note_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.credit_adjustment_id, credit.credit_adjustment_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertIsNone(stored.issued_invoice_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.tax_exclusive_amount, first_credit_amount)
        self.assertEqual(stored.tax_amount, Decimal("0"))
        self.assertEqual(stored.tax_inclusive_amount, first_credit_amount)
        self.assertEqual(stored.issued_credit_note_status, "issued")
        self.assertEqual(stored.credit_reason_code, "goodwill")
        self.assertEqual(stored.issued_at, issued_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.tax_inclusive_amount, Decimal)
        self.assertNotIsInstance(stored.tax_inclusive_amount, float)
        self.assertEqual(
            self.ledger.find_issued_credit_note(
                stored.tenant_account_id, stored.credit_adjustment_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_issued_credit_notes_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_issued_credit_notes_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_issued_credit_note(uuid4()))
        self.assertIsNone(
            self.ledger.find_issued_credit_note(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.issued_credit_note_id)

        replay = IssuedCreditNoteService(
            self.ledger, clock=lambda: issued_at
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(replay.issued_credit_note_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.issued_credit_note_id, stored.issued_credit_note_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
                ]
            ),
            1,
        )

        rejected = IssuedCreditNoteService(self.ledger).issue_credit_note(TENANT_ONE, uuid4())
        self.assertEqual(rejected.issued_credit_note_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note"
            ).fetchone()[0],
            1,
        )
        mismatch = IssuedCreditNoteService(self.ledger).issue_credit_note(
            TENANT_TWO, credit.credit_adjustment_id
        )
        self.assertEqual(mismatch.issued_credit_note_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note"
            ).fetchone()[0],
            1,
        )

        issued = IssuedInvoiceService(self.ledger, clock=lambda: issued_at).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        later_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, later_credit_amount, "billing_error"
        )
        self.assertEqual(later_credit.credit_adjustment_outcome_code.value, "accepted")
        assert later_credit.credit_adjustment_id is not None
        later = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, later_credit.credit_adjustment_id)
        self.assertEqual(later.issued_credit_note_outcome_code.value, "accepted")
        assert later.issued_credit_note_id is not None
        later_stored = self.ledger.get_issued_credit_note(later.issued_credit_note_id)
        assert later_stored is not None
        self.assertEqual(later_stored.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note"
            ).fetchone()[0],
            2,
        )

        crash_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, crash_credit_amount, "rating_correction"
        )
        self.assertEqual(crash_credit.credit_adjustment_outcome_code.value, "accepted")
        assert crash_credit.credit_adjustment_id is not None
        stored_crash_credit = self.ledger.get_credit_adjustment(crash_credit.credit_adjustment_id)
        assert stored_crash_credit is not None
        crash_hash = compute_issued_credit_note_payload_hash(
            {
                "credit_adjustment_id": str(stored_crash_credit.credit_adjustment_id),
                "credit_adjustment_contract_version": (
                    stored_crash_credit.credit_adjustment_contract_version
                ),
                "credit_adjustment_source_payload_hash": stored_crash_credit.source_payload_hash,
                "currency_code": stored_crash_credit.currency_code,
                "invoice_draft_id": str(stored_crash_credit.invoice_draft_id),
                "issued_credit_note_contract_version": 1,
                "tax_amount": format_exact_decimal(stored_crash_credit.tax_amount),
                "tax_exclusive_amount": format_exact_decimal(
                    stored_crash_credit.tax_exclusive_amount
                ),
                "tax_inclusive_amount": format_exact_decimal(stored_crash_credit.credit_amount),
                "issued_invoice_id": str(issued.issued_invoice_id),
            }
        )
        inserted_without_outbox = self.ledger.insert_issued_credit_note(
            StoredIssuedCreditNote(
                issued_credit_note_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                credit_adjustment_id=stored_crash_credit.credit_adjustment_id,
                invoice_draft_id=stored_crash_credit.invoice_draft_id,
                issued_invoice_id=issued.issued_invoice_id,
                issued_credit_note_contract_version=1,
                credit_adjustment_contract_version=(
                    stored_crash_credit.credit_adjustment_contract_version
                ),
                credit_reason_code=stored_crash_credit.credit_reason_code,
                credit_adjustment_source_payload_hash=stored_crash_credit.source_payload_hash,
                source_payload_hash=crash_hash,
                currency_code=stored_crash_credit.currency_code,
                tax_exclusive_amount=stored_crash_credit.tax_exclusive_amount,
                tax_amount=stored_crash_credit.tax_amount,
                tax_inclusive_amount=stored_crash_credit.credit_amount,
                issued_credit_note_status="issued",
                issued_at=datetime(2026, 8, 18, 17, 0, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
            ]
        )
        healed = IssuedCreditNoteService(
            self.ledger, clock=lambda: issued_at
        ).issue_credit_note(TENANT_ONE, crash_credit.credit_adjustment_id)
        self.assertEqual(healed.issued_credit_note_outcome_code.value, "duplicate_replay")
        self.assertEqual(healed.issued_credit_note_id, inserted_without_outbox.issued_credit_note_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
                ]
            ),
            prior_outbox + 1,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_issued_credit_note(stored.issued_credit_note_id)
        self.assertEqual(reloaded, stored)
        presentment = IssuedCreditNotePresentmentService(fresh).present_issued_credit_note(
            TENANT_ONE, stored.issued_credit_note_id
        )
        self.assertEqual(presentment.tax_inclusive_amount, first_credit_amount)
        self.assertEqual(presentment.issued_credit_note_status, "issued")
        self.assertEqual(presentment.next_operator_action, "wait")
        self.assertIsNone(presentment.issued_invoice_id)
        page = IssuedCreditNotePresentmentService(fresh).list_issued_credit_notes(TENANT_ONE)
        self.assertEqual(
            {row.issued_credit_note_id for row in page.issued_credit_notes},
            {
                stored.issued_credit_note_id,
                later.issued_credit_note_id,
                inserted_without_outbox.issued_credit_note_id,
            },
        )
        later_presentment = IssuedCreditNotePresentmentService(fresh).present_issued_credit_note(
            TENANT_ONE, later.issued_credit_note_id
        )
        self.assertEqual(later_presentment.issued_invoice_id, issued.issued_invoice_id)
        reloaded_presentment = IssuedCreditNotePresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_issued_credit_note(TENANT_ONE, stored.issued_credit_note_id)
        self.assertEqual(
            reloaded_presentment.issued_credit_note_id, presentment.issued_credit_note_id
        )
        with self.assertRaises(IssuedCreditNotePresentmentQueryError) as missing_pin:
            IssuedCreditNotePresentmentService(fresh).present_issued_credit_note(
                "", stored.issued_credit_note_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(IssuedCreditNotePresentmentQueryError) as other_pin:
            IssuedCreditNotePresentmentService(fresh).present_issued_credit_note(
                TENANT_TWO, stored.issued_credit_note_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "issued_credit_note_not_found")

        self.assertEqual(self.ledger.insert_issued_credit_note(stored), stored)
        self.assertEqual(
            self.ledger.insert_issued_credit_note(replace(stored, issued_credit_note_id=uuid4())),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(replace(stored, source_payload_hash="md5:abc"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(
                replace(stored, credit_adjustment_source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(replace(stored, issued_credit_note_status="posted"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(replace(stored, credit_reason_code="courtesy"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(
                replace(stored, tax_inclusive_amount=Decimal("2.00"), issued_credit_note_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note(
                replace(
                    stored,
                    issued_credit_note_id=later.issued_credit_note_id,
                    credit_adjustment_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_issued_credit_note(self, *args, **kwargs):
                return None

        raced = IssuedCreditNoteService(
            BlindFindLedger(self.connection), clock=lambda: issued_at
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(raced.issued_credit_note_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.issued_credit_note_id, stored.issued_credit_note_id)

    def test_issued_credit_note_void_is_durable(self) -> None:
        """Persist one issued_credit_note_void and keep GET presentment after restart."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        first_credit_amount = Decimal("0.001")
        later_credit_amount = Decimal("0.0005")
        crash_credit_amount = Decimal("0.00025")
        credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, first_credit_amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        issued_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        issued_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: issued_at
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(issued_note.issued_credit_note_outcome_code.value, "accepted")
        assert issued_note.issued_credit_note_id is not None
        voided_at = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
        accepted = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_credit_note(TENANT_ONE, issued_note.issued_credit_note_id)
        self.assertEqual(accepted.issued_credit_note_void_outcome_code.value, "accepted")
        assert accepted.issued_credit_note_void_id is not None
        stored = self.ledger.get_issued_credit_note_void(accepted.issued_credit_note_void_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.issued_credit_note_id, issued_note.issued_credit_note_id)
        self.assertEqual(stored.credit_adjustment_id, credit.credit_adjustment_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertIsNone(stored.issued_invoice_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.voided_amount, first_credit_amount)
        self.assertEqual(stored.issued_credit_note_void_status, "recorded")
        self.assertEqual(stored.voided_at, voided_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.voided_amount, Decimal)
        self.assertNotIsInstance(stored.voided_amount, float)
        reloaded_note = self.ledger.get_issued_credit_note(issued_note.issued_credit_note_id)
        assert reloaded_note is not None
        self.assertEqual(reloaded_note.issued_credit_note_status, "issued")
        self.assertEqual(
            self.ledger.find_issued_credit_note_void(
                stored.tenant_account_id, stored.issued_credit_note_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_issued_credit_note_voids_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_issued_credit_note_voids_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_issued_credit_note_void(uuid4()))
        self.assertIsNone(
            self.ledger.find_issued_credit_note_void(stored.tenant_account_id, uuid4())
        )
        self.assertIsNone(
            self.ledger.find_credit_note_application(
                stored.tenant_account_id, stored.issued_credit_note_id
            )
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_VOIDED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.issued_credit_note_void_id)

        replay = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_credit_note(TENANT_ONE, issued_note.issued_credit_note_id)
        self.assertEqual(replay.issued_credit_note_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.issued_credit_note_void_id, stored.issued_credit_note_void_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_VOIDED
                ]
            ),
            1,
        )

        rejected = IssuedCreditNoteVoidService(self.ledger).void_issued_credit_note(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.issued_credit_note_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            1,
        )
        mismatch = IssuedCreditNoteVoidService(self.ledger).void_issued_credit_note(
            TENANT_TWO, issued_note.issued_credit_note_id
        )
        self.assertEqual(mismatch.issued_credit_note_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            1,
        )
        issued = IssuedInvoiceService(self.ledger, clock=lambda: issued_at).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        later_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, later_credit_amount, "billing_error"
        )
        self.assertEqual(later_credit.credit_adjustment_outcome_code.value, "accepted")
        assert later_credit.credit_adjustment_id is not None
        later_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, later_credit.credit_adjustment_id)
        self.assertEqual(later_note.issued_credit_note_outcome_code.value, "accepted")
        assert later_note.issued_credit_note_id is not None
        currency_mismatch = IssuedCreditNoteVoidService(self.ledger).void_issued_credit_note(
            TENANT_ONE, later_note.issued_credit_note_id, currency_code="EUR"
        )
        self.assertEqual(currency_mismatch.issued_credit_note_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            1,
        )
        later = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, later_note.issued_credit_note_id)
        self.assertEqual(later.issued_credit_note_void_outcome_code.value, "accepted")
        assert later.issued_credit_note_void_id is not None
        later_stored = self.ledger.get_issued_credit_note_void(later.issued_credit_note_void_id)
        assert later_stored is not None
        self.assertEqual(later_stored.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(later_stored.voided_amount, later_credit_amount)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            2,
        )

        crash_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, crash_credit_amount, "rating_correction"
        )
        self.assertEqual(crash_credit.credit_adjustment_outcome_code.value, "accepted")
        assert crash_credit.credit_adjustment_id is not None
        crash_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, crash_credit.credit_adjustment_id)
        self.assertEqual(crash_note.issued_credit_note_outcome_code.value, "accepted")
        assert crash_note.issued_credit_note_id is not None
        stored_crash_note = self.ledger.get_issued_credit_note(crash_note.issued_credit_note_id)
        assert stored_crash_note is not None
        crash_hash = compute_issued_credit_note_void_payload_hash(
            {
                "issued_credit_note_id": str(stored_crash_note.issued_credit_note_id),
                "credit_adjustment_id": str(stored_crash_note.credit_adjustment_id),
                "invoice_draft_id": str(stored_crash_note.invoice_draft_id),
                "currency_code": stored_crash_note.currency_code,
                "voided_amount": format_exact_decimal(stored_crash_note.tax_inclusive_amount),
                "issued_credit_note_void_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_issued_credit_note_void(
            StoredIssuedCreditNoteVoid(
                issued_credit_note_void_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                issued_credit_note_id=stored_crash_note.issued_credit_note_id,
                credit_adjustment_id=stored_crash_note.credit_adjustment_id,
                invoice_draft_id=stored_crash_note.invoice_draft_id,
                issued_invoice_id=stored_crash_note.issued_invoice_id,
                issued_credit_note_void_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=stored_crash_note.currency_code,
                voided_amount=stored_crash_note.tax_inclusive_amount,
                issued_credit_note_void_status="recorded",
                voided_at=datetime(2026, 8, 18, 17, 30, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_VOIDED
            ]
        )
        healed = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_credit_note(TENANT_ONE, crash_note.issued_credit_note_id)
        self.assertEqual(healed.issued_credit_note_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.issued_credit_note_void_id, inserted_without_outbox.issued_credit_note_void_id
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_VOIDED
                ]
            ),
            prior_outbox + 1,
        )

        applied_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, Decimal("0.0001"), "goodwill"
        )
        self.assertEqual(applied_credit.credit_adjustment_outcome_code.value, "accepted")
        assert applied_credit.credit_adjustment_id is not None
        applied_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, applied_credit.credit_adjustment_id)
        self.assertEqual(applied_note.issued_credit_note_outcome_code.value, "accepted")
        assert applied_note.issued_credit_note_id is not None
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        self.connection.execute(
            """
            INSERT INTO billing_core.credit_note_application
                (credit_note_application_id, tenant_account_id, issued_credit_note_id,
                 collection_case_id, invoice_draft_id, issued_invoice_id,
                 credit_note_application_contract_version,
                 issued_credit_note_contract_version, source_payload_hash,
                 issued_credit_note_source_payload_hash, currency_code,
                 applied_amount, credit_note_application_status, applied_at)
            VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s, %s, 'USD', %s, 'applied', %s)
            """,
            (
                uuid4(),
                tenant.tenant_account_id,
                applied_note.issued_credit_note_id,
                collection.collection_case_id,
                draft.invoice_draft_id,
                issued.issued_invoice_id,
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                Decimal("0.0001"),
                datetime(2026, 8, 18, 18, 15, tzinfo=UTC),
            ),
        )
        self.connection.commit()
        applied = self.ledger.find_credit_note_application(
            tenant.tenant_account_id, applied_note.issued_credit_note_id
        )
        self.assertIsNotNone(applied)
        already_applied = IssuedCreditNoteVoidService(self.ledger).void_issued_credit_note(
            TENANT_ONE, applied_note.issued_credit_note_id
        )
        self.assertEqual(already_applied.issued_credit_note_void_outcome_code.value, "rejected")
        self.assertEqual(
            already_applied.rejection_reason_code.value, "credit_note_already_applied"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_credit_note_void"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_issued_credit_note_void(stored.issued_credit_note_void_id)
        self.assertEqual(reloaded, stored)
        presentment = IssuedCreditNoteVoidPresentmentService(fresh).present_issued_credit_note_void(
            TENANT_ONE, stored.issued_credit_note_void_id
        )
        self.assertEqual(presentment.voided_amount, first_credit_amount)
        self.assertEqual(presentment.issued_credit_note_void_status, "recorded")
        self.assertEqual(presentment.next_operator_action, "wait")
        self.assertIsNone(presentment.issued_invoice_id)
        page = IssuedCreditNoteVoidPresentmentService(fresh).list_issued_credit_note_voids(
            TENANT_ONE
        )
        self.assertEqual(
            {row.issued_credit_note_void_id for row in page.issued_credit_note_voids},
            {
                stored.issued_credit_note_void_id,
                later.issued_credit_note_void_id,
                inserted_without_outbox.issued_credit_note_void_id,
            },
        )
        later_presentment = IssuedCreditNoteVoidPresentmentService(
            fresh
        ).present_issued_credit_note_void(TENANT_ONE, later.issued_credit_note_void_id)
        self.assertEqual(later_presentment.issued_invoice_id, issued.issued_invoice_id)
        reloaded_presentment = IssuedCreditNoteVoidPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_issued_credit_note_void(TENANT_ONE, stored.issued_credit_note_void_id)
        self.assertEqual(
            reloaded_presentment.issued_credit_note_void_id,
            presentment.issued_credit_note_void_id,
        )
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as missing_pin:
            IssuedCreditNoteVoidPresentmentService(fresh).present_issued_credit_note_void(
                "", stored.issued_credit_note_void_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(IssuedCreditNoteVoidPresentmentQueryError) as other_pin:
            IssuedCreditNoteVoidPresentmentService(fresh).present_issued_credit_note_void(
                TENANT_TWO, stored.issued_credit_note_void_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "issued_credit_note_void_not_found"
        )

        self.assertEqual(self.ledger.insert_issued_credit_note_void(stored), stored)
        self.assertEqual(
            self.ledger.insert_issued_credit_note_void(
                replace(stored, issued_credit_note_void_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note_void(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note_void(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note_void(
                replace(stored, issued_credit_note_void_status="posted")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note_void(
                replace(stored, voided_amount=Decimal("0"), issued_credit_note_void_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_credit_note_void(
                replace(
                    stored,
                    issued_credit_note_void_id=later.issued_credit_note_void_id,
                    issued_credit_note_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_issued_credit_note_void(self, *args, **kwargs):
                return None

        raced = IssuedCreditNoteVoidService(
            BlindFindLedger(self.connection), clock=lambda: voided_at
        ).void_issued_credit_note(TENANT_ONE, issued_note.issued_credit_note_id)
        self.assertEqual(raced.issued_credit_note_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.issued_credit_note_void_id, stored.issued_credit_note_void_id)

    def test_credit_note_application_is_durable(self) -> None:
        """Persist one credit_note_application and keep GET presentment after restart."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        stored_draft = self.ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        self.assertGreater(stored_draft.drafted_total_amount, Decimal("0.00175"))
        first_credit_amount = Decimal("0.001")
        later_credit_amount = Decimal("0.0005")
        crash_credit_amount = Decimal("0.00025")
        credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, first_credit_amount, "goodwill"
        )
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")
        assert credit.credit_adjustment_id is not None
        issued_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, credit.credit_adjustment_id)
        self.assertEqual(issued_note.issued_credit_note_outcome_code.value, "accepted")
        assert issued_note.issued_credit_note_id is not None
        issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 14, 30, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        later_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, later_credit_amount, "billing_error"
        )
        self.assertEqual(later_credit.credit_adjustment_outcome_code.value, "accepted")
        assert later_credit.credit_adjustment_id is not None
        later_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, later_credit.credit_adjustment_id)
        self.assertEqual(later_note.issued_credit_note_outcome_code.value, "accepted")
        assert later_note.issued_credit_note_id is not None
        crash_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, crash_credit_amount, "rating_correction"
        )
        self.assertEqual(crash_credit.credit_adjustment_outcome_code.value, "accepted")
        assert crash_credit.credit_adjustment_id is not None
        crash_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, crash_credit.credit_adjustment_id)
        self.assertEqual(crash_note.issued_credit_note_outcome_code.value, "accepted")
        assert crash_note.issued_credit_note_id is not None
        stored_crash_note = self.ledger.get_issued_credit_note(crash_note.issued_credit_note_id)
        assert stored_crash_note is not None
        void_credit = CreditAdjustmentService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_credit_adjustment(
            TENANT_ONE, draft.invoice_draft_id, Decimal("0.0001"), "goodwill"
        )
        self.assertEqual(void_credit.credit_adjustment_outcome_code.value, "accepted")
        assert void_credit.credit_adjustment_id is not None
        void_note = IssuedCreditNoteService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_credit_note(TENANT_ONE, void_credit.credit_adjustment_id)
        self.assertEqual(void_note.issued_credit_note_outcome_code.value, "accepted")
        assert void_note.issued_credit_note_id is not None
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        self.assertEqual(collection.outstanding_amount, stored_draft.drafted_total_amount)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        accepted = CreditNoteApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_credit_note(
            TENANT_ONE, issued_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(accepted.credit_note_application_outcome_code.value, "accepted")
        assert accepted.credit_note_application_id is not None
        stored = self.ledger.get_credit_note_application(accepted.credit_note_application_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.issued_credit_note_id, issued_note.issued_credit_note_id)
        self.assertEqual(stored.collection_case_id, collection.collection_case_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertIsNone(stored.issued_invoice_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.applied_amount, first_credit_amount)
        self.assertEqual(stored.credit_note_application_status, "applied")
        self.assertEqual(stored.applied_at, applied_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.applied_amount, Decimal)
        self.assertNotIsInstance(stored.applied_amount, float)
        remaining_after_first = stored_draft.drafted_total_amount - first_credit_amount
        self.assertEqual(accepted.remaining_outstanding_amount, remaining_after_first)
        self.assertEqual(
            self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining_after_first,
        )
        self.assertEqual(
            self.ledger.find_credit_note_application(
                stored.tenant_account_id, stored.issued_credit_note_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_credit_note_applications_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_credit_note_applications_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_credit_note_application(uuid4()))
        self.assertIsNone(
            self.ledger.find_credit_note_application(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.credit_note_application_id)

        replay = CreditNoteApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_credit_note(
            TENANT_ONE, issued_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(replay.credit_note_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.credit_note_application_id, stored.credit_note_application_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining_after_first,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
                ]
            ),
            1,
        )

        rejected = CreditNoteApplicationService(self.ledger).apply_credit_note(
            TENANT_ONE, uuid4(), collection.collection_case_id
        )
        self.assertEqual(rejected.credit_note_application_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            1,
        )
        mismatch = CreditNoteApplicationService(self.ledger).apply_credit_note(
            TENANT_TWO, issued_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(mismatch.credit_note_application_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            1,
        )

        later = CreditNoteApplicationService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        ).apply_credit_note(
            TENANT_ONE, later_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(later.credit_note_application_outcome_code.value, "accepted")
        assert later.credit_note_application_id is not None
        later_stored = self.ledger.get_credit_note_application(later.credit_note_application_id)
        assert later_stored is not None
        self.assertEqual(later_stored.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(later_stored.applied_amount, later_credit_amount)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            2,
        )

        crash_hash = compute_application_payload_hash(
            {
                "collection_case_id": str(collection.collection_case_id),
                "credit_note_application_contract_version": 1,
                "currency_code": stored_crash_note.currency_code,
                "applied_amount": format_exact_decimal(stored_crash_note.tax_inclusive_amount),
                "invoice_draft_id": str(stored_crash_note.invoice_draft_id),
                "issued_credit_note_contract_version": (
                    stored_crash_note.issued_credit_note_contract_version
                ),
                "issued_credit_note_id": str(stored_crash_note.issued_credit_note_id),
                "issued_invoice_id": str(issued.issued_invoice_id),
            }
        )
        inserted_without_outbox = self.ledger.insert_credit_note_application(
            StoredCreditNoteApplication(
                credit_note_application_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                issued_credit_note_id=stored_crash_note.issued_credit_note_id,
                collection_case_id=collection.collection_case_id,
                invoice_draft_id=stored_crash_note.invoice_draft_id,
                issued_invoice_id=issued.issued_invoice_id,
                credit_note_application_contract_version=1,
                issued_credit_note_contract_version=(
                    stored_crash_note.issued_credit_note_contract_version
                ),
                source_payload_hash=crash_hash,
                issued_credit_note_source_payload_hash=stored_crash_note.source_payload_hash,
                currency_code=stored_crash_note.currency_code,
                applied_amount=stored_crash_note.tax_inclusive_amount,
                credit_note_application_status="applied",
                applied_at=datetime(2026, 8, 18, 17, 15, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
            ]
        )
        healed = CreditNoteApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_credit_note(
            TENANT_ONE, crash_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(healed.credit_note_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.credit_note_application_id, inserted_without_outbox.credit_note_application_id
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_APPLIED
                ]
            ),
            prior_outbox + 1,
        )

        voided = IssuedCreditNoteVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 15, tzinfo=UTC)
        ).void_issued_credit_note(TENANT_ONE, void_note.issued_credit_note_id)
        self.assertEqual(voided.issued_credit_note_void_outcome_code.value, "accepted")
        already_voided = CreditNoteApplicationService(self.ledger).apply_credit_note(
            TENANT_ONE, void_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(already_voided.credit_note_application_outcome_code.value, "rejected")
        self.assertEqual(
            already_voided.rejection_reason_code.value, "issued_credit_note_voided"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.credit_note_application"
            ).fetchone()[0],
            3,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_credit_note_application(stored.credit_note_application_id)
        self.assertEqual(reloaded, stored)
        presentment = CreditNoteApplicationPresentmentService(
            fresh
        ).present_credit_note_application(TENANT_ONE, stored.credit_note_application_id)
        self.assertEqual(presentment.applied_amount, first_credit_amount)
        self.assertEqual(presentment.credit_note_application_status, "applied")
        self.assertEqual(presentment.next_operator_action, "collect")
        self.assertIsNone(presentment.issued_invoice_id)
        page = CreditNoteApplicationPresentmentService(fresh).list_credit_note_applications(
            TENANT_ONE
        )
        self.assertEqual(
            {row.credit_note_application_id for row in page.credit_note_applications},
            {
                stored.credit_note_application_id,
                later.credit_note_application_id,
                inserted_without_outbox.credit_note_application_id,
            },
        )
        later_presentment = CreditNoteApplicationPresentmentService(
            fresh
        ).present_credit_note_application(TENANT_ONE, later.credit_note_application_id)
        self.assertEqual(later_presentment.issued_invoice_id, issued.issued_invoice_id)
        reloaded_presentment = CreditNoteApplicationPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_credit_note_application(TENANT_ONE, stored.credit_note_application_id)
        self.assertEqual(
            reloaded_presentment.credit_note_application_id,
            presentment.credit_note_application_id,
        )
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as missing_pin:
            CreditNoteApplicationPresentmentService(fresh).present_credit_note_application(
                "", stored.credit_note_application_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(CreditNoteApplicationPresentmentQueryError) as other_pin:
            CreditNoteApplicationPresentmentService(fresh).present_credit_note_application(
                TENANT_TWO, stored.credit_note_application_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "credit_note_application_not_found"
        )

        self.assertEqual(self.ledger.insert_credit_note_application(stored), stored)
        self.assertEqual(
            self.ledger.insert_credit_note_application(
                replace(stored, credit_note_application_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(
                replace(stored, issued_credit_note_source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(
                replace(stored, credit_note_application_status="open")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(
                replace(stored, applied_amount=Decimal("0"), credit_note_application_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_credit_note_application(
                replace(
                    stored,
                    credit_note_application_id=later.credit_note_application_id,
                    issued_credit_note_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_credit_note_application(self, *args, **kwargs):
                return None

        raced = CreditNoteApplicationService(
            BlindFindLedger(self.connection), clock=lambda: applied_at
        ).apply_credit_note(
            TENANT_ONE, issued_note.issued_credit_note_id, collection.collection_case_id
        )
        self.assertEqual(raced.credit_note_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.credit_note_application_id, stored.credit_note_application_id)
        self.assertEqual(
            self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining_after_first - later_credit_amount,
        )

    def test_issued_invoice_void_is_durable(self) -> None:
        """Persist one issued_invoice_void and keep GET presentment after restart."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        issued_at = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
        issued = IssuedInvoiceService(self.ledger, clock=lambda: issued_at).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        assert issued.issued_invoice_id is not None
        first_voided_amount = issued.tax_inclusive_amount
        self.assertGreater(first_voided_amount, Decimal("0"))
        self.assertNotEqual(first_voided_amount, KNOWN_MORNING_TOTAL)
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        voided_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        accepted = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        self.assertEqual(accepted.issued_invoice_void_outcome_code.value, "accepted")
        assert accepted.issued_invoice_void_id is not None
        stored = self.ledger.get_issued_invoice_void(accepted.issued_invoice_void_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.issued_invoice_id, issued.issued_invoice_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertEqual(stored.collection_case_id, collection.collection_case_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.voided_amount, first_voided_amount)
        self.assertEqual(stored.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(stored.issued_invoice_void_status, "recorded")
        self.assertEqual(stored.voided_at, voided_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.voided_amount, Decimal)
        self.assertNotIsInstance(stored.voided_amount, float)
        reloaded_invoice = self.ledger.get_issued_invoice(issued.issued_invoice_id)
        assert reloaded_invoice is not None
        self.assertEqual(reloaded_invoice.issued_invoice_status, "issued")
        voided_case = self.ledger.get_collection_case(collection.collection_case_id)
        assert voided_case is not None
        self.assertEqual(voided_case.collection_case_status, "voided")
        self.assertEqual(voided_case.outstanding_amount, Decimal("0"))
        self.assertEqual(
            self.ledger.find_issued_invoice_void(
                stored.tenant_account_id, stored.issued_invoice_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_issued_invoice_voids_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_issued_invoice_voids_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_issued_invoice_void(uuid4()))
        self.assertIsNone(
            self.ledger.find_issued_invoice_void(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.issued_invoice_void_id)

        replay = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, issued.issued_invoice_id)
        self.assertEqual(replay.issued_invoice_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.issued_invoice_void_id, stored.issued_invoice_void_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
                ]
            ),
            1,
        )
        replayed_case = self.ledger.get_collection_case(collection.collection_case_id)
        assert replayed_case is not None
        self.assertEqual(replayed_case.collection_case_status, "voided")
        self.assertEqual(replayed_case.outstanding_amount, Decimal("0"))

        rejected = IssuedInvoiceVoidService(self.ledger).void_issued_invoice(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.issued_invoice_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            1,
        )
        mismatch = IssuedInvoiceVoidService(self.ledger).void_issued_invoice(
            TENANT_TWO, issued.issued_invoice_id
        )
        self.assertEqual(mismatch.issued_invoice_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            1,
        )

        afternoon = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf71c",
            source_event_key="workflow_381:step_06:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(afternoon)
        afternoon_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        afternoon_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, afternoon_window, 1, rate_card_code="cwl_standard"
        )
        afternoon_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, afternoon_rating.rating_run_id
        )
        afternoon_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, afternoon_draft.invoice_draft_id)
        self.assertEqual(afternoon_issued.issued_invoice_outcome_code.value, "accepted")
        assert afternoon_issued.issued_invoice_id is not None
        currency_mismatch = IssuedInvoiceVoidService(self.ledger).void_issued_invoice(
            TENANT_ONE, afternoon_issued.issued_invoice_id, currency_code="EUR"
        )
        self.assertEqual(currency_mismatch.issued_invoice_void_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            1,
        )
        stored_afternoon = self.ledger.get_issued_invoice(afternoon_issued.issued_invoice_id)
        assert stored_afternoon is not None
        crash_hash = compute_issued_invoice_void_payload_hash(
            {
                "issued_invoice_id": str(stored_afternoon.issued_invoice_id),
                "invoice_draft_id": str(stored_afternoon.invoice_draft_id),
                "currency_code": stored_afternoon.currency_code,
                "voided_amount": format_exact_decimal(stored_afternoon.tax_inclusive_amount),
                "issued_invoice_void_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_issued_invoice_void(
            StoredIssuedInvoiceVoid(
                issued_invoice_void_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                issued_invoice_id=stored_afternoon.issued_invoice_id,
                invoice_draft_id=stored_afternoon.invoice_draft_id,
                collection_case_id=None,
                issued_invoice_void_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=stored_afternoon.currency_code,
                voided_amount=stored_afternoon.tax_inclusive_amount,
                remaining_outstanding_amount=Decimal("0"),
                issued_invoice_void_status="recorded",
                voided_at=datetime(2026, 8, 18, 16, 30, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
            ]
        )
        healed = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, afternoon_issued.issued_invoice_id)
        self.assertEqual(healed.issued_invoice_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.issued_invoice_void_id, inserted_without_outbox.issued_invoice_void_id
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_INVOICE_VOIDED
                ]
            ),
            prior_outbox + 1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            2,
        )

        evening = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf81c",
            source_event_key="workflow_381:step_07:attempt_01",
            occurred_at="2026-08-16T12:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "200",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(evening)
        evening_window = TimeWindow.from_iso8601(
            "2026-08-16T12:00:00Z", "2026-08-16T13:00:00Z"
        )
        evening_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, evening_window, 1, rate_card_code="cwl_standard"
        )
        evening_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, evening_rating.rating_run_id
        )
        evening_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, evening_draft.invoice_draft_id)
        self.assertEqual(evening_issued.issued_invoice_outcome_code.value, "accepted")
        evening_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, evening_draft.invoice_draft_id)
        self.assertEqual(evening_collection.collection_case_outcome_code.value, "accepted")
        assert evening_collection.collection_case_id is not None
        evening_amount = evening_issued.tax_inclusive_amount
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_voided(uuid4(), evening_amount)
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_voided(
                evening_collection.collection_case_id, Decimal("1.00")
            )
        already_voided = self.ledger.mark_collection_case_voided(
            collection.collection_case_id, first_voided_amount
        )
        self.assertEqual(already_voided.collection_case_status, "voided")
        self.ledger.apply_collection_settlement(
            evening_collection.collection_case_id, evening_amount
        )
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_voided(
                evening_collection.collection_case_id, evening_amount
            )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_issued_invoice_void(stored.issued_invoice_void_id)
        self.assertEqual(reloaded, stored)
        presentment = IssuedInvoiceVoidPresentmentService(fresh).present_issued_invoice_void(
            TENANT_ONE, stored.issued_invoice_void_id
        )
        self.assertEqual(presentment.voided_amount, first_voided_amount)
        self.assertEqual(presentment.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(presentment.issued_invoice_void_status, "recorded")
        self.assertEqual(presentment.collection_case_status, "voided")
        self.assertEqual(presentment.next_operator_action, "wait")
        page = IssuedInvoiceVoidPresentmentService(fresh).list_issued_invoice_voids(TENANT_ONE)
        self.assertEqual(
            {row.issued_invoice_void_id for row in page.issued_invoice_voids},
            {
                stored.issued_invoice_void_id,
                inserted_without_outbox.issued_invoice_void_id,
            },
        )
        reloaded_presentment = IssuedInvoiceVoidPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_issued_invoice_void(TENANT_ONE, stored.issued_invoice_void_id)
        self.assertEqual(
            reloaded_presentment.issued_invoice_void_id,
            presentment.issued_invoice_void_id,
        )
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as missing_pin:
            IssuedInvoiceVoidPresentmentService(fresh).present_issued_invoice_void(
                "", stored.issued_invoice_void_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(IssuedInvoiceVoidPresentmentQueryError) as other_pin:
            IssuedInvoiceVoidPresentmentService(fresh).present_issued_invoice_void(
                TENANT_TWO, stored.issued_invoice_void_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "issued_invoice_void_not_found"
        )

        self.assertEqual(self.ledger.insert_issued_invoice_void(stored), stored)
        self.assertEqual(
            self.ledger.insert_issued_invoice_void(
                replace(stored, issued_invoice_void_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(
                replace(stored, issued_invoice_void_status="posted")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(
                replace(
                    stored,
                    remaining_outstanding_amount=Decimal("1"),
                    issued_invoice_void_id=uuid4(),
                    issued_invoice_id=uuid4(),
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(
                replace(stored, voided_amount=Decimal("0"), issued_invoice_void_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_issued_invoice_void(
                replace(
                    stored,
                    issued_invoice_void_id=inserted_without_outbox.issued_invoice_void_id,
                    issued_invoice_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        race_event = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf91c",
            source_event_key="workflow_381:step_08:attempt_01",
            occurred_at="2026-08-16T13:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "100",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(race_event)
        race_window = TimeWindow.from_iso8601(
            "2026-08-16T13:00:00Z", "2026-08-16T14:00:00Z"
        )
        race_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, race_window, 1, rate_card_code="cwl_standard"
        )
        race_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, race_rating.rating_run_id
        )
        race_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, race_draft.invoice_draft_id)
        self.assertEqual(race_issued.issued_invoice_outcome_code.value, "accepted")
        assert race_issued.issued_invoice_id is not None
        race_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, race_draft.invoice_draft_id)
        self.assertEqual(race_collection.collection_case_outcome_code.value, "accepted")
        assert race_collection.collection_case_id is not None
        stored_race_invoice = self.ledger.get_issued_invoice(race_issued.issued_invoice_id)
        assert stored_race_invoice is not None
        race_hash = compute_issued_invoice_void_payload_hash(
            {
                "issued_invoice_id": str(stored_race_invoice.issued_invoice_id),
                "invoice_draft_id": str(stored_race_invoice.invoice_draft_id),
                "currency_code": stored_race_invoice.currency_code,
                "voided_amount": format_exact_decimal(
                    stored_race_invoice.tax_inclusive_amount
                ),
                "issued_invoice_void_contract_version": 1,
            }
        )
        raced_inserted = self.ledger.insert_issued_invoice_void(
            StoredIssuedInvoiceVoid(
                issued_invoice_void_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                issued_invoice_id=stored_race_invoice.issued_invoice_id,
                invoice_draft_id=stored_race_invoice.invoice_draft_id,
                collection_case_id=race_collection.collection_case_id,
                issued_invoice_void_contract_version=1,
                source_payload_hash=race_hash,
                currency_code=stored_race_invoice.currency_code,
                voided_amount=stored_race_invoice.tax_inclusive_amount,
                remaining_outstanding_amount=Decimal("0"),
                issued_invoice_void_status="recorded",
                voided_at=datetime(2026, 8, 18, 18, 30, tzinfo=UTC),
            )
        )
        open_race_case = self.ledger.get_collection_case(race_collection.collection_case_id)
        assert open_race_case is not None
        self.assertEqual(open_race_case.collection_case_status, "open")

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_issued_invoice_void(self, *args, **kwargs):
                return None

        raced = IssuedInvoiceVoidService(
            BlindFindLedger(self.connection), clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, race_issued.issued_invoice_id)
        self.assertEqual(raced.issued_invoice_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.issued_invoice_void_id, raced_inserted.issued_invoice_void_id)
        raced_case = self.ledger.get_collection_case(race_collection.collection_case_id)
        assert raced_case is not None
        self.assertEqual(raced_case.collection_case_status, "voided")
        self.assertEqual(raced_case.outstanding_amount, Decimal("0"))
        self.assertEqual(raced.collection_case_status, "voided")
        self.assertEqual(raced.remaining_outstanding_amount, Decimal("0"))
        raced_without_case = IssuedInvoiceVoidService(
            BlindFindLedger(self.connection), clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, afternoon_issued.issued_invoice_id)
        self.assertEqual(
            raced_without_case.issued_invoice_void_outcome_code.value, "duplicate_replay"
        )
        self.assertEqual(
            raced_without_case.issued_invoice_void_id,
            inserted_without_outbox.issued_invoice_void_id,
        )

        class BlindFindMissingCaseLedger(PostgresUsageLedger):
            """Force the insert race when the stored case cannot be loaded."""

            def find_issued_invoice_void(self, *args, **kwargs):
                return None

            def get_collection_case(self, *args, **kwargs):
                return None

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            3,
        )

        night = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfa1c",
            source_event_key="workflow_381:step_09:attempt_01",
            occurred_at="2026-08-16T14:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "250",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(night)
        night_window = TimeWindow.from_iso8601(
            "2026-08-16T14:00:00Z", "2026-08-16T15:00:00Z"
        )
        night_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, night_window, 1, rate_card_code="cwl_standard"
        )
        night_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, night_rating.rating_run_id
        )
        night_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, night_draft.invoice_draft_id)
        self.assertEqual(night_issued.issued_invoice_outcome_code.value, "accepted")
        assert night_issued.issued_invoice_id is not None
        night_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, night_draft.invoice_draft_id)
        self.assertEqual(night_collection.collection_case_outcome_code.value, "accepted")
        assert night_collection.collection_case_id is not None
        stored_night_invoice = self.ledger.get_issued_invoice(night_issued.issued_invoice_id)
        assert stored_night_invoice is not None
        night_hash = compute_issued_invoice_void_payload_hash(
            {
                "issued_invoice_id": str(stored_night_invoice.issued_invoice_id),
                "invoice_draft_id": str(stored_night_invoice.invoice_draft_id),
                "currency_code": stored_night_invoice.currency_code,
                "voided_amount": format_exact_decimal(
                    stored_night_invoice.tax_inclusive_amount
                ),
                "issued_invoice_void_contract_version": 1,
            }
        )
        inserted_without_close = self.ledger.insert_issued_invoice_void(
            StoredIssuedInvoiceVoid(
                issued_invoice_void_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                issued_invoice_id=stored_night_invoice.issued_invoice_id,
                invoice_draft_id=stored_night_invoice.invoice_draft_id,
                collection_case_id=night_collection.collection_case_id,
                issued_invoice_void_contract_version=1,
                source_payload_hash=night_hash,
                currency_code=stored_night_invoice.currency_code,
                voided_amount=stored_night_invoice.tax_inclusive_amount,
                remaining_outstanding_amount=Decimal("0"),
                issued_invoice_void_status="recorded",
                voided_at=datetime(2026, 8, 18, 19, 30, tzinfo=UTC),
            )
        )
        unclosed_case = self.ledger.get_collection_case(night_collection.collection_case_id)
        assert unclosed_case is not None
        self.assertEqual(unclosed_case.collection_case_status, "open")
        self.assertEqual(
            unclosed_case.outstanding_amount, stored_night_invoice.tax_inclusive_amount
        )
        missing_case = IssuedInvoiceVoidService(
            BlindFindMissingCaseLedger(self.connection), clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, night_issued.issued_invoice_id)
        self.assertEqual(missing_case.issued_invoice_void_outcome_code.value, "rejected")
        self.assertEqual(
            missing_case.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND,
        )
        still_unclosed = self.ledger.get_collection_case(night_collection.collection_case_id)
        assert still_unclosed is not None
        self.assertEqual(still_unclosed.collection_case_status, "open")
        self.assertEqual(
            still_unclosed.outstanding_amount, stored_night_invoice.tax_inclusive_amount
        )
        healed_case = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, night_issued.issued_invoice_id)
        self.assertEqual(healed_case.issued_invoice_void_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed_case.issued_invoice_void_id,
            inserted_without_close.issued_invoice_void_id,
        )
        self.assertEqual(healed_case.collection_case_status, "voided")
        self.assertEqual(healed_case.remaining_outstanding_amount, Decimal("0"))
        closed_night = self.ledger.get_collection_case(night_collection.collection_case_id)
        assert closed_night is not None
        self.assertEqual(closed_night.collection_case_status, "voided")
        self.assertEqual(closed_night.outstanding_amount, Decimal("0"))
        already_healed = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: voided_at
        ).void_issued_invoice(TENANT_ONE, night_issued.issued_invoice_id)
        self.assertEqual(
            already_healed.issued_invoice_void_outcome_code.value, "duplicate_replay"
        )
        self.assertEqual(already_healed.collection_case_status, "voided")
        self.assertEqual(already_healed.remaining_outstanding_amount, Decimal("0"))
        still_voided = self.ledger.get_collection_case(night_collection.collection_case_id)
        assert still_voided is not None
        self.assertEqual(still_voided.collection_case_status, "voided")
        self.assertEqual(still_voided.outstanding_amount, Decimal("0"))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.issued_invoice_void"
            ).fetchone()[0],
            4,
        )

    def test_unapplied_cash_is_durable(self) -> None:
        """Persist one parked leftover and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(intent.payment_intent_outcome_code.value, "accepted")
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        remaining_before = received
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        prior_outbox = len(
            self.ledger.list_webhook_outbox_events_for_tenant(
                self.ledger.require_tenant(TENANT_ONE).tenant_account_id
            )
        )
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        accepted = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(accepted.unapplied_cash_outcome_code.value, "accepted")
        assert accepted.unapplied_cash_id is not None
        stored = self.ledger.get_unapplied_cash(accepted.unapplied_cash_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(stored.payment_intent_id, intent.payment_intent_id)
        self.assertEqual(stored.collection_case_id, collection.collection_case_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.unapplied_amount, leftover)
        self.assertEqual(stored.received_amount, received)
        self.assertEqual(stored.applied_amount, received)
        self.assertEqual(stored.unapplied_cash_status, "parked")
        self.assertEqual(stored.parked_at, parked_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.unapplied_amount, Decimal)
        self.assertNotIsInstance(stored.unapplied_amount, float)
        reloaded_receipt = self.ledger.get_payment_receipt(receipt.payment_receipt_id)
        assert reloaded_receipt is not None
        self.assertEqual(reloaded_receipt.received_amount, received)
        parked_case = self.ledger.get_collection_case(collection.collection_case_id)
        assert parked_case is not None
        self.assertEqual(parked_case.outstanding_amount, remaining_before - received)
        self.assertEqual(
            self.ledger.find_unapplied_cash(stored.tenant_account_id, stored.payment_receipt_id),
            stored,
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_unapplied_cash(uuid4()))
        self.assertIsNone(self.ledger.find_unapplied_cash(stored.tenant_account_id, uuid4()))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(self.ledger.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)),
            prior_outbox,
        )

        replay = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(replay.unapplied_cash_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.unapplied_cash_id, stored.unapplied_cash_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining_before - received,
        )
        self.assertEqual(
            len(self.ledger.list_webhook_outbox_events_for_tenant(stored.tenant_account_id)),
            prior_outbox,
        )

        rejected = UnappliedCashService(self.ledger).park_unapplied_cash(
            TENANT_ONE, uuid4(), leftover
        )
        self.assertEqual(rejected.unapplied_cash_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        mismatch = UnappliedCashService(self.ledger).park_unapplied_cash(
            TENANT_TWO, receipt.payment_receipt_id, leftover
        )
        self.assertEqual(mismatch.unapplied_cash_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        afternoon = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf71c",
            source_event_key="workflow_381:step_06:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(afternoon)
        afternoon_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        afternoon_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, afternoon_window, 1, rate_card_code="cwl_standard"
        )
        afternoon_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, afternoon_rating.rating_run_id
        )
        afternoon_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, afternoon_draft.invoice_draft_id)
        assert afternoon_collection.collection_case_id is not None
        afternoon_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, afternoon_collection.collection_case_id)
        assert afternoon_intent.payment_intent_id is not None
        afternoon_received = self.ledger.get_collection_case(
            afternoon_collection.collection_case_id
        ).outstanding_amount
        afternoon_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).record_payment_receipt(
            TENANT_ONE, afternoon_intent.payment_intent_id, afternoon_received
        )
        self.assertEqual(afternoon_receipt.payment_settlement_outcome_code.value, "accepted")
        assert afternoon_receipt.payment_receipt_id is not None
        omitted = UnappliedCashService(self.ledger).park_unapplied_cash(
            TENANT_ONE, afternoon_receipt.payment_receipt_id
        )
        self.assertEqual(omitted.unapplied_cash_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        currency_mismatch = UnappliedCashService(self.ledger).park_unapplied_cash(
            TENANT_ONE,
            afternoon_receipt.payment_receipt_id,
            leftover,
            currency_code="EUR",
        )
        self.assertEqual(currency_mismatch.unapplied_cash_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            1,
        )
        later = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, afternoon_receipt.payment_receipt_id, leftover)
        self.assertEqual(later.unapplied_cash_outcome_code.value, "accepted")
        assert later.unapplied_cash_id is not None
        later_stored = self.ledger.get_unapplied_cash(later.unapplied_cash_id)
        assert later_stored is not None
        self.assertEqual(later_stored.payment_receipt_id, afternoon_receipt.payment_receipt_id)
        self.assertEqual(later_stored.unapplied_amount, leftover)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            2,
        )

        self.assertEqual(self.ledger.insert_unapplied_cash(stored), stored)
        self.assertEqual(
            self.ledger.insert_unapplied_cash(replace(stored, unapplied_cash_id=uuid4())),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(replace(stored, source_payload_hash="md5:abc"))
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(stored, unapplied_cash_status="applied")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(stored, unapplied_amount=Decimal("0"), unapplied_cash_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(
                    stored,
                    unapplied_amount=received + leftover,
                    unapplied_cash_id=uuid4(),
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(stored, received_amount=Decimal("0"), unapplied_cash_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(stored, applied_amount=Decimal("0"), unapplied_cash_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash(
                replace(
                    stored,
                    unapplied_cash_id=later.unapplied_cash_id,
                    payment_receipt_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_unapplied_cash(self, *args, **kwargs):
                return None

        raced = UnappliedCashService(
            BlindFindLedger(self.connection), clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(raced.unapplied_cash_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.unapplied_cash_id, stored.unapplied_cash_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash"
            ).fetchone()[0],
            2,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_unapplied_cash(stored.unapplied_cash_id)
        self.assertEqual(reloaded, stored)
        presentment = UnappliedCashPresentmentService(fresh).present_unapplied_cash(
            TENANT_ONE, stored.unapplied_cash_id
        )
        self.assertEqual(presentment.unapplied_amount, leftover)
        self.assertEqual(presentment.unapplied_cash_status, "parked")
        self.assertEqual(presentment.next_operator_action, "wait")
        self.assertEqual(presentment.received_amount, received)
        page = UnappliedCashPresentmentService(fresh).list_unapplied_cash(TENANT_ONE)
        self.assertEqual(
            {row.unapplied_cash_id for row in page.unapplied_cash},
            {stored.unapplied_cash_id, later.unapplied_cash_id},
        )
        reloaded_presentment = UnappliedCashPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_unapplied_cash(TENANT_ONE, stored.unapplied_cash_id)
        self.assertEqual(
            reloaded_presentment.unapplied_cash_id, presentment.unapplied_cash_id
        )
        with self.assertRaises(UnappliedCashPresentmentQueryError) as missing_pin:
            UnappliedCashPresentmentService(fresh).present_unapplied_cash(
                "", stored.unapplied_cash_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(UnappliedCashPresentmentQueryError) as other_pin:
            UnappliedCashPresentmentService(fresh).present_unapplied_cash(
                TENANT_TWO, stored.unapplied_cash_id
            )
        self.assertEqual(other_pin.exception.rejection_reason_code, "unapplied_cash_not_found")

    def test_unapplied_cash_application_is_durable(self) -> None:
        """Persist one leftover-apply and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(intent.payment_intent_outcome_code.value, "accepted")
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None
        stored_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert stored_leftover is not None
        self.assertEqual(stored_leftover.unapplied_cash_status, "parked")

        afternoon = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf71c",
            source_event_key="workflow_381:step_06:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(afternoon)
        afternoon_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        afternoon_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, afternoon_window, 1, rate_card_code="cwl_standard"
        )
        afternoon_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, afternoon_rating.rating_run_id
        )
        afternoon_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, afternoon_draft.invoice_draft_id)
        assert afternoon_collection.collection_case_id is not None
        afternoon_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, afternoon_draft.invoice_draft_id)
        self.assertEqual(afternoon_issued.issued_invoice_outcome_code.value, "accepted")
        afternoon_remaining = self.ledger.get_collection_case(
            afternoon_collection.collection_case_id
        ).outstanding_amount
        self.assertEqual(afternoon_remaining, leftover)
        applied_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        accepted = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, afternoon_collection.collection_case_id
        )
        self.assertEqual(accepted.unapplied_cash_application_outcome_code.value, "accepted")
        assert accepted.unapplied_cash_application_id is not None
        stored = self.ledger.get_unapplied_cash_application(accepted.unapplied_cash_application_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.unapplied_cash_id, parked.unapplied_cash_id)
        self.assertEqual(stored.collection_case_id, afternoon_collection.collection_case_id)
        self.assertEqual(stored.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(stored.invoice_draft_id, afternoon_draft.invoice_draft_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.applied_amount, leftover)
        self.assertEqual(stored.unapplied_cash_application_status, "applied")
        self.assertEqual(stored.applied_at, applied_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.applied_amount, Decimal)
        self.assertNotIsInstance(stored.applied_amount, float)
        applied_case = self.ledger.get_collection_case(afternoon_collection.collection_case_id)
        assert applied_case is not None
        self.assertEqual(applied_case.outstanding_amount, Decimal("0"))
        self.assertEqual(applied_case.collection_case_status, "open")
        self.assertEqual(accepted.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(accepted.next_operator_action, "settle")
        reloaded_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert reloaded_leftover is not None
        self.assertEqual(reloaded_leftover.unapplied_cash_status, "parked")
        self.assertEqual(
            self.ledger.find_unapplied_cash_application(
                stored.tenant_account_id, stored.unapplied_cash_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_applications_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_applications_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_unapplied_cash_application(uuid4()))
        self.assertIsNone(
            self.ledger.find_unapplied_cash_application(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.unapplied_cash_application_id)

        replay = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, afternoon_collection.collection_case_id
        )
        self.assertEqual(replay.unapplied_cash_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.unapplied_cash_application_id, stored.unapplied_cash_application_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.get_collection_case(afternoon_collection.collection_case_id).outstanding_amount,
            Decimal("0"),
        )
        self.assertEqual(
            self.ledger.get_collection_case(afternoon_collection.collection_case_id).collection_case_status,
            "open",
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
                ]
            ),
            1,
        )

        rejected = UnappliedCashApplicationService(self.ledger).apply_unapplied_cash(
            TENANT_ONE, uuid4(), afternoon_collection.collection_case_id
        )
        self.assertEqual(rejected.unapplied_cash_application_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            1,
        )
        mismatch = UnappliedCashApplicationService(self.ledger).apply_unapplied_cash(
            TENANT_TWO, parked.unapplied_cash_id, afternoon_collection.collection_case_id
        )
        self.assertEqual(mismatch.unapplied_cash_application_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            1,
        )
        settled_source = UnappliedCashApplicationService(self.ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(settled_source.unapplied_cash_application_outcome_code.value, "duplicate_replay")

        unused_void = IssuedInvoiceVoidService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
        ).void_issued_invoice(TENANT_ONE, afternoon_issued.issued_invoice_id)
        self.assertEqual(unused_void.issued_invoice_void_outcome_code.value, "rejected")
        self.assertEqual(
            unused_void.rejection_reason_code,
            IssuedInvoiceVoidRejectionReasonCode.UNAPPLIED_CASH_ALREADY_APPLIED,
        )
        self.assertEqual(
            self.ledger.get_collection_case(afternoon_collection.collection_case_id).collection_case_status,
            "open",
        )

        evening = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf81c",
            source_event_key="workflow_381:step_07:attempt_01",
            occurred_at="2026-08-16T13:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(evening)
        evening_window = TimeWindow.from_iso8601(
            "2026-08-16T13:00:00Z", "2026-08-16T14:00:00Z"
        )
        evening_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, evening_window, 1, rate_card_code="cwl_standard"
        )
        evening_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, evening_rating.rating_run_id
        )
        evening_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, evening_draft.invoice_draft_id)
        assert evening_collection.collection_case_id is not None
        evening_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, evening_collection.collection_case_id)
        assert evening_intent.payment_intent_id is not None
        evening_received = self.ledger.get_collection_case(
            evening_collection.collection_case_id
        ).outstanding_amount
        evening_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).record_payment_receipt(
            TENANT_ONE, evening_intent.payment_intent_id, evening_received
        )
        assert evening_receipt.payment_receipt_id is not None
        later_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, evening_receipt.payment_receipt_id, leftover)
        self.assertEqual(later_parked.unapplied_cash_outcome_code.value, "accepted")
        assert later_parked.unapplied_cash_id is not None
        night = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf91c",
            source_event_key="workflow_381:step_08:attempt_01",
            occurred_at="2026-08-16T15:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "2000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(night)
        night_window = TimeWindow.from_iso8601(
            "2026-08-16T15:00:00Z", "2026-08-16T16:00:00Z"
        )
        night_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, night_window, 1, rate_card_code="cwl_standard"
        )
        night_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, night_rating.rating_run_id
        )
        night_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, night_draft.invoice_draft_id)
        assert night_collection.collection_case_id is not None
        night_remaining_before = self.ledger.get_collection_case(
            night_collection.collection_case_id
        ).outstanding_amount
        later = UnappliedCashApplicationService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).apply_unapplied_cash(
            TENANT_ONE, later_parked.unapplied_cash_id, night_collection.collection_case_id
        )
        self.assertEqual(later.unapplied_cash_application_outcome_code.value, "accepted")
        assert later.unapplied_cash_application_id is not None
        later_stored = self.ledger.get_unapplied_cash_application(later.unapplied_cash_application_id)
        assert later_stored is not None
        self.assertEqual(later_stored.applied_amount, leftover)
        self.assertEqual(
            self.ledger.get_collection_case(night_collection.collection_case_id).outstanding_amount,
            night_remaining_before - leftover,
        )
        self.assertEqual(
            self.ledger.get_collection_case(night_collection.collection_case_id).collection_case_status,
            "open",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            2,
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfa1c",
            source_event_key="workflow_381:step_09:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_collection.collection_case_id is not None
        crash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, crash_collection.collection_case_id)
        assert crash_intent.payment_intent_id is not None
        crash_received = self.ledger.get_collection_case(
            crash_collection.collection_case_id
        ).outstanding_amount
        crash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).record_payment_receipt(
            TENANT_ONE, crash_intent.payment_intent_id, crash_received
        )
        assert crash_receipt.payment_receipt_id is not None
        crash_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 15, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, crash_receipt.payment_receipt_id, leftover)
        assert crash_parked.unapplied_cash_id is not None
        crash_leftover = self.ledger.get_unapplied_cash(crash_parked.unapplied_cash_id)
        assert crash_leftover is not None
        crash_target = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfb1c",
            source_event_key="workflow_381:step_10:attempt_01",
            occurred_at="2026-08-16T19:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "2500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_target)
        crash_target_window = TimeWindow.from_iso8601(
            "2026-08-16T19:00:00Z", "2026-08-16T20:00:00Z"
        )
        crash_target_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_target_window, 1, rate_card_code="cwl_standard"
        )
        crash_target_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_target_rating.rating_run_id
        )
        crash_target_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_target_draft.invoice_draft_id)
        assert crash_target_collection.collection_case_id is not None
        crash_target_issued = IssuedInvoiceService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 20, tzinfo=UTC)
        ).issue_invoice(TENANT_ONE, crash_target_draft.invoice_draft_id)
        self.assertEqual(crash_target_issued.issued_invoice_outcome_code.value, "accepted")
        crash_target_remaining_before = self.ledger.get_collection_case(
            crash_target_collection.collection_case_id
        ).outstanding_amount
        crash_hash = compute_unapplied_cash_application_payload_hash(
            {
                "unapplied_cash_id": str(crash_leftover.unapplied_cash_id),
                "collection_case_id": str(crash_target_collection.collection_case_id),
                "payment_receipt_id": str(crash_leftover.payment_receipt_id),
                "currency_code": crash_leftover.currency_code,
                "applied_amount": format_exact_decimal(leftover),
                "unapplied_amount": format_exact_decimal(crash_leftover.unapplied_amount),
                "unapplied_cash_application_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_unapplied_cash_application(
            StoredUnappliedCashApplication(
                unapplied_cash_application_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                unapplied_cash_id=crash_leftover.unapplied_cash_id,
                collection_case_id=crash_target_collection.collection_case_id,
                payment_receipt_id=crash_leftover.payment_receipt_id,
                invoice_draft_id=crash_target_draft.invoice_draft_id,
                unapplied_cash_application_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=crash_leftover.currency_code,
                applied_amount=leftover,
                unapplied_cash_application_status="applied",
                applied_at=datetime(2026, 8, 18, 17, 30, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
            ]
        )
        healed = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, crash_parked.unapplied_cash_id, crash_target_collection.collection_case_id
        )
        self.assertEqual(healed.unapplied_cash_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.unapplied_cash_application_id,
            inserted_without_outbox.unapplied_cash_application_id,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_UNAPPLIED_CASH_APPLIED
                ]
            ),
            prior_outbox + 1,
        )
        healed_case = self.ledger.get_collection_case(crash_target_collection.collection_case_id)
        assert healed_case is not None
        self.assertEqual(
            healed_case.outstanding_amount, crash_target_remaining_before - leftover
        )
        self.assertEqual(healed_case.collection_case_status, "open")
        self.assertEqual(
            healed.remaining_outstanding_amount, crash_target_remaining_before - leftover
        )
        already_healed_remaining = UnappliedCashApplicationService(
            self.ledger, clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, crash_parked.unapplied_cash_id, crash_target_collection.collection_case_id
        )
        self.assertEqual(
            already_healed_remaining.unapplied_cash_application_outcome_code.value,
            "duplicate_replay",
        )
        still_healed_case = self.ledger.get_collection_case(
            crash_target_collection.collection_case_id
        )
        assert still_healed_case is not None
        self.assertEqual(
            still_healed_case.outstanding_amount, crash_target_remaining_before - leftover
        )
        self.assertEqual(still_healed_case.collection_case_status, "open")

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_unapplied_cash_application(stored.unapplied_cash_application_id)
        self.assertEqual(reloaded, stored)
        presentment = UnappliedCashApplicationPresentmentService(
            fresh
        ).present_unapplied_cash_application(TENANT_ONE, stored.unapplied_cash_application_id)
        self.assertEqual(presentment.applied_amount, leftover)
        self.assertEqual(presentment.unapplied_cash_application_status, "applied")
        self.assertEqual(presentment.next_operator_action, "settle")
        self.assertEqual(presentment.remaining_outstanding_amount, Decimal("0"))
        page = UnappliedCashApplicationPresentmentService(fresh).list_unapplied_cash_applications(
            TENANT_ONE
        )
        self.assertEqual(
            {row.unapplied_cash_application_id for row in page.unapplied_cash_applications},
            {
                stored.unapplied_cash_application_id,
                later.unapplied_cash_application_id,
                inserted_without_outbox.unapplied_cash_application_id,
            },
        )
        later_presentment = UnappliedCashApplicationPresentmentService(
            fresh
        ).present_unapplied_cash_application(TENANT_ONE, later.unapplied_cash_application_id)
        self.assertEqual(later_presentment.next_operator_action, "collect")
        reloaded_presentment = UnappliedCashApplicationPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_unapplied_cash_application(TENANT_ONE, stored.unapplied_cash_application_id)
        self.assertEqual(
            reloaded_presentment.unapplied_cash_application_id,
            presentment.unapplied_cash_application_id,
        )
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as missing_pin:
            UnappliedCashApplicationPresentmentService(fresh).present_unapplied_cash_application(
                "", stored.unapplied_cash_application_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(UnappliedCashApplicationPresentmentQueryError) as other_pin:
            UnappliedCashApplicationPresentmentService(fresh).present_unapplied_cash_application(
                TENANT_TWO, stored.unapplied_cash_application_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "unapplied_cash_application_not_found"
        )

        self.assertEqual(self.ledger.insert_unapplied_cash_application(stored), stored)
        self.assertEqual(
            self.ledger.insert_unapplied_cash_application(
                replace(stored, unapplied_cash_application_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_application(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_application(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_application(
                replace(stored, unapplied_cash_application_status="parked")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_application(
                replace(stored, applied_amount=Decimal("0"), unapplied_cash_application_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_application(
                replace(
                    stored,
                    unapplied_cash_application_id=later.unapplied_cash_application_id,
                    unapplied_cash_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.apply_unapplied_cash_to_collection_case(uuid4(), leftover)
        with self.assertRaises(ValueError):
            self.ledger.apply_unapplied_cash_to_collection_case(
                afternoon_collection.collection_case_id, Decimal("0")
            )
        with self.assertRaises(ValueError):
            self.ledger.apply_unapplied_cash_to_collection_case(
                collection.collection_case_id, leftover
            )
        with self.assertRaises(ValueError):
            self.ledger.apply_unapplied_cash_to_collection_case(
                afternoon_collection.collection_case_id, leftover
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_unapplied_cash_application(self, *args, **kwargs):
                return None

        raced = UnappliedCashApplicationService(
            BlindFindLedger(self.connection), clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, later_parked.unapplied_cash_id, night_collection.collection_case_id
        )
        self.assertEqual(raced.unapplied_cash_application_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.unapplied_cash_application_id, later.unapplied_cash_application_id)
        self.assertEqual(
            self.ledger.get_collection_case(night_collection.collection_case_id).outstanding_amount,
            night_remaining_before - leftover,
        )

        class BlindFindMissingCaseLedger(PostgresUsageLedger):
            """Force the insert race when the stored case cannot be loaded."""

            def __init__(self, connection: object) -> None:
                super().__init__(connection)
                self._hide_collection_case = False

            def find_unapplied_cash_application(self, *args, **kwargs):
                return None

            def insert_unapplied_cash_application(self, application):
                stored_application = PostgresUsageLedger.insert_unapplied_cash_application(
                    self, application
                )
                self._hide_collection_case = True
                return stored_application

            def get_collection_case(self, collection_case_id):
                if self._hide_collection_case:
                    return None
                return PostgresUsageLedger.get_collection_case(self, collection_case_id)

        missing_case = UnappliedCashApplicationService(
            BlindFindMissingCaseLedger(self.connection), clock=lambda: applied_at
        ).apply_unapplied_cash(
            TENANT_ONE, later_parked.unapplied_cash_id, night_collection.collection_case_id
        )
        self.assertEqual(missing_case.unapplied_cash_application_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            3,
        )

    def test_unapplied_cash_refund_is_durable(self) -> None:
        """Persist one leftover refund and keep GET presentment after restart."""
        leftover = Decimal("0.001")
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(intent.payment_intent_outcome_code.value, "accepted")
        assert intent.payment_intent_id is not None
        received = self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount
        self.assertGreater(received, leftover)
        self.assertNotEqual(received, KNOWN_MORNING_TOTAL)
        receipt = PaymentSettlementService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_payment_receipt(TENANT_ONE, intent.payment_intent_id, received)
        self.assertEqual(receipt.payment_settlement_outcome_code.value, "accepted")
        assert receipt.payment_receipt_id is not None
        parked_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        parked = UnappliedCashService(
            self.ledger, clock=lambda: parked_at
        ).park_unapplied_cash(TENANT_ONE, receipt.payment_receipt_id, leftover)
        self.assertEqual(parked.unapplied_cash_outcome_code.value, "accepted")
        assert parked.unapplied_cash_id is not None
        stored_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert stored_leftover is not None
        self.assertEqual(stored_leftover.unapplied_cash_status, "parked")

        refunded_at = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
        accepted = UnappliedCashRefundService(
            self.ledger, clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(accepted.unapplied_cash_refund_outcome_code.value, "accepted")
        assert accepted.unapplied_cash_refund_id is not None
        stored = self.ledger.get_unapplied_cash_refund(accepted.unapplied_cash_refund_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.unapplied_cash_id, parked.unapplied_cash_id)
        self.assertEqual(stored.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(stored.payment_intent_id, intent.payment_intent_id)
        self.assertEqual(stored.collection_case_id, collection.collection_case_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.refund_amount, leftover)
        self.assertEqual(stored.unapplied_amount, leftover)
        self.assertEqual(stored.unapplied_cash_refund_status, "recorded")
        self.assertEqual(stored.refunded_at, refunded_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.refund_amount, Decimal)
        self.assertNotIsInstance(stored.refund_amount, float)
        reloaded_leftover = self.ledger.get_unapplied_cash(parked.unapplied_cash_id)
        assert reloaded_leftover is not None
        self.assertEqual(reloaded_leftover.unapplied_cash_status, "parked")
        self.assertEqual(reloaded_leftover.unapplied_amount, leftover)
        self.assertEqual(
            self.ledger.find_unapplied_cash_refund(
                stored.tenant_account_id, stored.unapplied_cash_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_refunds_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_unapplied_cash_refunds_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_unapplied_cash_refund(uuid4()))
        self.assertIsNone(
            self.ledger.find_unapplied_cash_refund(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_REFUND_RECORDED
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.unapplied_cash_refund_id)

        replay = UnappliedCashRefundService(
            self.ledger, clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(replay.unapplied_cash_refund_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.unapplied_cash_refund_id, stored.unapplied_cash_refund_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.get_unapplied_cash(parked.unapplied_cash_id).unapplied_cash_status,
            "parked",
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_REFUND_RECORDED
                ]
            ),
            1,
        )

        rejected = UnappliedCashRefundService(self.ledger).refund_unapplied_cash(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.unapplied_cash_refund_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            1,
        )
        mismatch = UnappliedCashRefundService(self.ledger).refund_unapplied_cash(
            TENANT_TWO, parked.unapplied_cash_id
        )
        self.assertEqual(mismatch.unapplied_cash_refund_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            1,
        )

        afternoon = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfc1c",
            source_event_key="workflow_381:step_11:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(afternoon)
        afternoon_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        afternoon_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, afternoon_window, 1, rate_card_code="cwl_standard"
        )
        afternoon_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, afternoon_rating.rating_run_id
        )
        afternoon_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, afternoon_draft.invoice_draft_id)
        assert afternoon_collection.collection_case_id is not None
        already_refunded = UnappliedCashApplicationService(self.ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, afternoon_collection.collection_case_id
        )
        self.assertEqual(already_refunded.unapplied_cash_application_outcome_code.value, "rejected")
        self.assertEqual(
            already_refunded.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_ALREADY_REFUNDED,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_application"
            ).fetchone()[0],
            0,
        )

        evening = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfd1c",
            source_event_key="workflow_381:step_12:attempt_01",
            occurred_at="2026-08-16T13:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(evening)
        evening_window = TimeWindow.from_iso8601(
            "2026-08-16T13:00:00Z", "2026-08-16T14:00:00Z"
        )
        evening_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, evening_window, 1, rate_card_code="cwl_standard"
        )
        evening_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, evening_rating.rating_run_id
        )
        evening_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, evening_draft.invoice_draft_id)
        assert evening_collection.collection_case_id is not None
        evening_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, evening_collection.collection_case_id)
        assert evening_intent.payment_intent_id is not None
        evening_received = self.ledger.get_collection_case(
            evening_collection.collection_case_id
        ).outstanding_amount
        evening_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        ).record_payment_receipt(
            TENANT_ONE, evening_intent.payment_intent_id, evening_received
        )
        assert evening_receipt.payment_receipt_id is not None
        later_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 15, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, evening_receipt.payment_receipt_id, leftover)
        self.assertEqual(later_parked.unapplied_cash_outcome_code.value, "accepted")
        assert later_parked.unapplied_cash_id is not None
        later = UnappliedCashRefundService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).refund_unapplied_cash(TENANT_ONE, later_parked.unapplied_cash_id)
        self.assertEqual(later.unapplied_cash_refund_outcome_code.value, "accepted")
        assert later.unapplied_cash_refund_id is not None
        later_stored = self.ledger.get_unapplied_cash_refund(later.unapplied_cash_refund_id)
        assert later_stored is not None
        self.assertEqual(later_stored.refund_amount, leftover)
        self.assertEqual(
            self.ledger.get_unapplied_cash(later_parked.unapplied_cash_id).unapplied_cash_status,
            "parked",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            2,
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfe1c",
            source_event_key="workflow_381:step_13:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_collection.collection_case_id is not None
        crash_intent = PaymentIntentService(
            self.ledger, clock=lambda: CATALOG_START
        ).project_payment_intent(TENANT_ONE, crash_collection.collection_case_id)
        assert crash_intent.payment_intent_id is not None
        crash_received = self.ledger.get_collection_case(
            crash_collection.collection_case_id
        ).outstanding_amount
        crash_receipt = PaymentSettlementService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 0, tzinfo=UTC)
        ).record_payment_receipt(
            TENANT_ONE, crash_intent.payment_intent_id, crash_received
        )
        assert crash_receipt.payment_receipt_id is not None
        crash_parked = UnappliedCashService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 17, 15, tzinfo=UTC)
        ).park_unapplied_cash(TENANT_ONE, crash_receipt.payment_receipt_id, leftover)
        assert crash_parked.unapplied_cash_id is not None
        crash_leftover = self.ledger.get_unapplied_cash(crash_parked.unapplied_cash_id)
        assert crash_leftover is not None
        crash_hash = compute_unapplied_cash_refund_payload_hash(
            {
                "unapplied_cash_id": str(crash_leftover.unapplied_cash_id),
                "payment_receipt_id": str(crash_leftover.payment_receipt_id),
                "currency_code": crash_leftover.currency_code,
                "refund_amount": format_exact_decimal(leftover),
                "unapplied_amount": format_exact_decimal(crash_leftover.unapplied_amount),
                "unapplied_cash_refund_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_unapplied_cash_refund(
            StoredUnappliedCashRefund(
                unapplied_cash_refund_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                unapplied_cash_id=crash_leftover.unapplied_cash_id,
                payment_receipt_id=crash_leftover.payment_receipt_id,
                payment_intent_id=crash_leftover.payment_intent_id,
                collection_case_id=crash_leftover.collection_case_id,
                unapplied_cash_refund_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=crash_leftover.currency_code,
                refund_amount=leftover,
                unapplied_amount=leftover,
                unapplied_cash_refund_status="recorded",
                refunded_at=datetime(2026, 8, 18, 17, 30, tzinfo=UTC),
            )
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_REFUND_RECORDED
            ]
        )
        healed = UnappliedCashRefundService(
            self.ledger, clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, crash_parked.unapplied_cash_id)
        self.assertEqual(healed.unapplied_cash_refund_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.unapplied_cash_refund_id,
            inserted_without_outbox.unapplied_cash_refund_id,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_REFUND_RECORDED
                ]
            ),
            prior_outbox + 1,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_unapplied_cash_refund(stored.unapplied_cash_refund_id)
        self.assertEqual(reloaded, stored)
        presentment = UnappliedCashRefundPresentmentService(fresh).present_unapplied_cash_refund(
            TENANT_ONE, stored.unapplied_cash_refund_id
        )
        self.assertEqual(presentment.refund_amount, leftover)
        self.assertEqual(presentment.unapplied_cash_refund_status, "recorded")
        self.assertEqual(presentment.unapplied_cash_status, "parked")
        self.assertEqual(presentment.next_operator_action, "wait")
        page = UnappliedCashRefundPresentmentService(fresh).list_unapplied_cash_refunds(
            TENANT_ONE
        )
        self.assertEqual(
            {row.unapplied_cash_refund_id for row in page.unapplied_cash_refunds},
            {
                stored.unapplied_cash_refund_id,
                later.unapplied_cash_refund_id,
                inserted_without_outbox.unapplied_cash_refund_id,
            },
        )
        reloaded_presentment = UnappliedCashRefundPresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_unapplied_cash_refund(TENANT_ONE, stored.unapplied_cash_refund_id)
        self.assertEqual(
            reloaded_presentment.unapplied_cash_refund_id,
            presentment.unapplied_cash_refund_id,
        )
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as missing_pin:
            UnappliedCashRefundPresentmentService(fresh).present_unapplied_cash_refund(
                "", stored.unapplied_cash_refund_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as other_pin:
            UnappliedCashRefundPresentmentService(fresh).present_unapplied_cash_refund(
                TENANT_TWO, stored.unapplied_cash_refund_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "unapplied_cash_refund_not_found"
        )

        self.assertEqual(self.ledger.insert_unapplied_cash_refund(stored), stored)
        self.assertEqual(
            self.ledger.insert_unapplied_cash_refund(
                replace(stored, unapplied_cash_refund_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(stored, unapplied_cash_refund_status="parked")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(stored, refund_amount=Decimal("0"), unapplied_cash_refund_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(stored, unapplied_amount=Decimal("0"), unapplied_cash_refund_id=uuid4())
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(
                    stored,
                    refund_amount=leftover + leftover,
                    unapplied_cash_refund_id=uuid4(),
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_unapplied_cash_refund(
                replace(
                    stored,
                    unapplied_cash_refund_id=later.unapplied_cash_refund_id,
                    unapplied_cash_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_unapplied_cash_refund(self, *args, **kwargs):
                return None

        raced = UnappliedCashRefundService(
            BlindFindLedger(self.connection), clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, later_parked.unapplied_cash_id)
        self.assertEqual(raced.unapplied_cash_refund_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.unapplied_cash_refund_id, later.unapplied_cash_refund_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            3,
        )

        class BlindFindMissingLeftoverLedger(PostgresUsageLedger):
            """Force the insert race when the stored leftover cannot be loaded."""

            def __init__(self, connection: object) -> None:
                super().__init__(connection)
                self._hide_leftover = False

            def find_unapplied_cash_refund(self, *args, **kwargs):
                return None

            def insert_unapplied_cash_refund(self, refund):
                stored_refund = PostgresUsageLedger.insert_unapplied_cash_refund(self, refund)
                self._hide_leftover = True
                return stored_refund

            def get_unapplied_cash(self, unapplied_cash_id):
                if self._hide_leftover:
                    return None
                return PostgresUsageLedger.get_unapplied_cash(self, unapplied_cash_id)

        missing_leftover = UnappliedCashRefundService(
            BlindFindMissingLeftoverLedger(self.connection), clock=lambda: refunded_at
        ).refund_unapplied_cash(TENANT_ONE, later_parked.unapplied_cash_id)
        self.assertEqual(missing_leftover.unapplied_cash_refund_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.unapplied_cash_refund"
            ).fetchone()[0],
            3,
        )

    def test_collection_dispute_is_durable(self) -> None:
        """Persist one dispute hold and keep GET presentment after restart."""
        UsageIngestionService(self.ledger).ingest_usage_event(make_event())
        RateCardService(self.ledger).publish_rate_card(
            TENANT_ONE,
            "cwl_standard",
            "USD",
            (
                {"metric_code": "gen_ai_output_token", "unit_amount": "0.000002", "currency_code": "USD"},
            ),
        )
        rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        draft = InvoiceDraftService(self.ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
        collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, draft.invoice_draft_id)
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        assert collection.collection_case_id is not None
        remaining = self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount
        self.assertGreater(remaining, 0)
        self.assertNotEqual(remaining, KNOWN_MORNING_TOTAL)

        held_at = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        accepted = CollectionDisputeService(
            self.ledger, clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(accepted.collection_dispute_outcome_code.value, "accepted")
        assert accepted.collection_dispute_id is not None
        stored = self.ledger.get_collection_dispute(accepted.collection_dispute_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        tenant = self.ledger.require_tenant(TENANT_ONE)
        self.assertEqual(stored.tenant_account_id, tenant.tenant_account_id)
        self.assertEqual(stored.collection_case_id, collection.collection_case_id)
        self.assertEqual(stored.invoice_draft_id, draft.invoice_draft_id)
        self.assertEqual(stored.currency_code, "USD")
        self.assertEqual(stored.remaining_outstanding_amount, remaining)
        self.assertEqual(stored.collection_dispute_status, "held")
        self.assertEqual(stored.held_at, held_at)
        self.assertIsNone(stored.released_at)
        self.assertEqual(stored.source_payload_hash, accepted.source_payload_hash)
        self.assertIsInstance(stored.remaining_outstanding_amount, Decimal)
        self.assertNotIsInstance(stored.remaining_outstanding_amount, float)
        reloaded_case = self.ledger.get_collection_case(collection.collection_case_id)
        assert reloaded_case is not None
        self.assertEqual(reloaded_case.collection_case_status, "disputed")
        self.assertEqual(reloaded_case.outstanding_amount, remaining)
        self.assertEqual(
            self.ledger.find_collection_dispute(
                stored.tenant_account_id, stored.collection_case_id
            ),
            stored,
        )
        self.assertEqual(
            self.ledger.list_collection_disputes_for_tenant(stored.tenant_account_id),
            (stored,),
        )
        self.assertEqual(
            self.ledger.list_collection_disputes_for_tenant(
                self.ledger.require_tenant(TENANT_TWO).tenant_account_id
            ),
            (),
        )
        self.assertIsNone(self.ledger.get_collection_dispute(uuid4()))
        self.assertIsNone(
            self.ledger.find_collection_dispute(stored.tenant_account_id, uuid4())
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            1,
        )
        outbox_rows = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_DISPUTE_HELD
        ]
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].source_id, stored.collection_dispute_id)

        replay = CollectionDisputeService(
            self.ledger, clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, collection.collection_case_id)
        self.assertEqual(replay.collection_dispute_outcome_code.value, "duplicate_replay")
        self.assertEqual(replay.collection_dispute_id, stored.collection_dispute_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.get_collection_case(collection.collection_case_id).outstanding_amount,
            remaining,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_DISPUTE_HELD
                ]
            ),
            1,
        )

        rejected = CollectionDisputeService(self.ledger).hold_collection_case(
            TENANT_ONE, uuid4()
        )
        self.assertEqual(rejected.collection_dispute_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            1,
        )
        mismatch = CollectionDisputeService(self.ledger).hold_collection_case(
            TENANT_TWO, collection.collection_case_id
        )
        self.assertEqual(mismatch.collection_dispute_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            1,
        )
        disputed_write_off = CollectionWriteOffService(self.ledger).write_off_collection_case(
            TENANT_ONE, collection.collection_case_id
        )
        self.assertEqual(disputed_write_off.collection_write_off_outcome_code.value, "rejected")
        self.assertEqual(
            disputed_write_off.rejection_reason_code,
            CollectionWriteOffRejectionReasonCode.COLLECTION_CASE_DISPUTED,
        )

        afternoon = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfc2c",
            source_event_key="workflow_381:step_21:attempt_01",
            occurred_at="2026-08-16T11:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(afternoon)
        afternoon_window = TimeWindow.from_iso8601(
            "2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z"
        )
        afternoon_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, afternoon_window, 1, rate_card_code="cwl_standard"
        )
        afternoon_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, afternoon_rating.rating_run_id
        )
        afternoon_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, afternoon_draft.invoice_draft_id)
        assert afternoon_collection.collection_case_id is not None
        afternoon_remaining = self.ledger.get_collection_case(
            afternoon_collection.collection_case_id
        ).outstanding_amount
        afternoon_held_at = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
        later = CollectionDisputeService(
            self.ledger, clock=lambda: afternoon_held_at
        ).hold_collection_case(TENANT_ONE, afternoon_collection.collection_case_id)
        self.assertEqual(later.collection_dispute_outcome_code.value, "accepted")
        assert later.collection_dispute_id is not None
        later_stored = self.ledger.get_collection_dispute(later.collection_dispute_id)
        assert later_stored is not None
        self.assertEqual(later_stored.remaining_outstanding_amount, afternoon_remaining)
        self.assertEqual(
            self.ledger.get_collection_case(afternoon_collection.collection_case_id).collection_case_status,
            "disputed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            2,
        )

        released_at = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
        released = CollectionDisputeReleaseService(
            self.ledger, clock=lambda: released_at
        ).release_collection_dispute(TENANT_ONE, later.collection_dispute_id)
        self.assertEqual(released.collection_dispute_release_outcome_code.value, "accepted")
        released_row = self.ledger.get_collection_dispute(later.collection_dispute_id)
        assert released_row is not None
        self.assertEqual(released_row.collection_dispute_status, "released")
        self.assertEqual(released_row.released_at, released_at)
        self.assertEqual(released_row.remaining_outstanding_amount, afternoon_remaining)
        self.assertEqual(
            self.ledger.get_collection_case(afternoon_collection.collection_case_id).collection_case_status,
            "open",
        )
        release_outbox = [
            event
            for event in self.ledger.list_webhook_outbox_events_for_tenant(
                stored.tenant_account_id
            )
            if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
        ]
        self.assertEqual(len(release_outbox), 1)
        self.assertEqual(release_outbox[0].source_id, later.collection_dispute_id)
        release_replay = CollectionDisputeReleaseService(
            self.ledger, clock=lambda: released_at
        ).release_collection_dispute(TENANT_ONE, later.collection_dispute_id)
        self.assertEqual(
            release_replay.collection_dispute_release_outcome_code.value, "duplicate_replay"
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
                ]
            ),
            1,
        )
        later_hold = CollectionDisputeService(self.ledger).hold_collection_case(
            TENANT_ONE, afternoon_collection.collection_case_id
        )
        self.assertEqual(later_hold.collection_dispute_outcome_code.value, "rejected")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            2,
        )

        crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bfe2c",
            source_event_key="workflow_381:step_22:attempt_01",
            occurred_at="2026-08-16T17:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "1500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(crash_usage)
        crash_window = TimeWindow.from_iso8601(
            "2026-08-16T17:00:00Z", "2026-08-16T18:00:00Z"
        )
        crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, crash_window, 1, rate_card_code="cwl_standard"
        )
        crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, crash_rating.rating_run_id
        )
        crash_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, crash_draft.invoice_draft_id)
        assert crash_collection.collection_case_id is not None
        crash_case = self.ledger.get_collection_case(crash_collection.collection_case_id)
        assert crash_case is not None
        crash_hash = compute_dispute_payload_hash(
            {
                "collection_case_id": str(crash_case.collection_case_id),
                "invoice_draft_id": str(crash_case.invoice_draft_id),
                "currency_code": crash_case.currency_code,
                "remaining_outstanding_amount": format_exact_decimal(
                    crash_case.outstanding_amount
                ),
                "collection_dispute_contract_version": 1,
            }
        )
        inserted_without_outbox = self.ledger.insert_collection_dispute(
            StoredCollectionDispute(
                collection_dispute_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                collection_case_id=crash_case.collection_case_id,
                invoice_draft_id=crash_case.invoice_draft_id,
                issued_invoice_id=None,
                collection_dispute_contract_version=1,
                source_payload_hash=crash_hash,
                currency_code=crash_case.currency_code,
                remaining_outstanding_amount=crash_case.outstanding_amount,
                collection_dispute_status="held",
                held_at=datetime(2026, 8, 18, 17, 30, tzinfo=UTC),
            )
        )
        self.assertEqual(
            self.ledger.get_collection_case(crash_case.collection_case_id).collection_case_status,
            "open",
        )
        prior_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_DISPUTE_HELD
            ]
        )
        healed = CollectionDisputeService(
            self.ledger, clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, crash_collection.collection_case_id)
        self.assertEqual(healed.collection_dispute_outcome_code.value, "duplicate_replay")
        self.assertEqual(
            healed.collection_dispute_id,
            inserted_without_outbox.collection_dispute_id,
        )
        self.assertEqual(
            self.ledger.get_collection_case(crash_case.collection_case_id).collection_case_status,
            "disputed",
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_DISPUTE_HELD
                ]
            ),
            prior_outbox + 1,
        )

        release_crash_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bff2c",
            source_event_key="workflow_381:step_23:attempt_01",
            occurred_at="2026-08-16T19:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "2000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(release_crash_usage)
        release_crash_window = TimeWindow.from_iso8601(
            "2026-08-16T19:00:00Z", "2026-08-16T20:00:00Z"
        )
        release_crash_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, release_crash_window, 1, rate_card_code="cwl_standard"
        )
        release_crash_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, release_crash_rating.rating_run_id
        )
        release_crash_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, release_crash_draft.invoice_draft_id)
        assert release_crash_collection.collection_case_id is not None
        crash_hold = CollectionDisputeService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        ).hold_collection_case(TENANT_ONE, release_crash_collection.collection_case_id)
        assert crash_hold.collection_dispute_id is not None
        crash_released_at = datetime(2026, 8, 18, 18, 30, tzinfo=UTC)
        self.ledger.mark_collection_dispute_released(
            crash_hold.collection_dispute_id, crash_released_at
        )
        self.assertEqual(
            self.ledger.get_collection_case(
                release_crash_collection.collection_case_id
            ).collection_case_status,
            "disputed",
        )
        prior_release_outbox = len(
            [
                event
                for event in self.ledger.list_webhook_outbox_events_for_tenant(
                    stored.tenant_account_id
                )
                if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
            ]
        )
        healed_release = CollectionDisputeReleaseService(
            self.ledger, clock=lambda: crash_released_at
        ).release_collection_dispute(TENANT_ONE, crash_hold.collection_dispute_id)
        self.assertEqual(
            healed_release.collection_dispute_release_outcome_code.value, "duplicate_replay"
        )
        self.assertEqual(
            self.ledger.get_collection_case(
                release_crash_collection.collection_case_id
            ).collection_case_status,
            "open",
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in self.ledger.list_webhook_outbox_events_for_tenant(
                        stored.tenant_account_id
                    )
                    if event.event_type_code == EVENT_TYPE_DISPUTE_RELEASED
                ]
            ),
            prior_release_outbox + 1,
        )

        fresh = PostgresUsageLedger(self.connection)
        reloaded = fresh.get_collection_dispute(stored.collection_dispute_id)
        self.assertEqual(reloaded, stored)
        presentment = CollectionDisputePresentmentService(fresh).present_collection_dispute(
            TENANT_ONE, stored.collection_dispute_id
        )
        self.assertEqual(presentment.remaining_outstanding_amount, remaining)
        self.assertEqual(presentment.collection_dispute_status, "held")
        self.assertEqual(presentment.collection_case_status, "disputed")
        self.assertEqual(presentment.next_operator_action, "wait")
        page = CollectionDisputePresentmentService(fresh).list_collection_disputes(TENANT_ONE)
        self.assertEqual(
            {row.collection_dispute_id for row in page.collection_disputes},
            {
                stored.collection_dispute_id,
                later.collection_dispute_id,
                inserted_without_outbox.collection_dispute_id,
                crash_hold.collection_dispute_id,
            },
        )
        release_presentment = CollectionDisputeReleasePresentmentService(
            fresh
        ).present_collection_dispute_release(TENANT_ONE, later.collection_dispute_id)
        self.assertEqual(release_presentment.collection_dispute_status, "released")
        self.assertEqual(release_presentment.collection_case_status, "open")
        reloaded_presentment = CollectionDisputePresentmentService(
            PostgresUsageLedger(self.connection)
        ).present_collection_dispute(TENANT_ONE, stored.collection_dispute_id)
        self.assertEqual(
            reloaded_presentment.collection_dispute_id,
            presentment.collection_dispute_id,
        )
        with self.assertRaises(CollectionDisputePresentmentQueryError) as missing_pin:
            CollectionDisputePresentmentService(fresh).present_collection_dispute(
                "", stored.collection_dispute_id
            )
        self.assertEqual(missing_pin.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(CollectionDisputePresentmentQueryError) as other_pin:
            CollectionDisputePresentmentService(fresh).present_collection_dispute(
                TENANT_TWO, stored.collection_dispute_id
            )
        self.assertEqual(
            other_pin.exception.rejection_reason_code, "collection_dispute_not_found"
        )
        with self.assertRaises(CollectionDisputeReleasePresentmentQueryError) as held_release:
            CollectionDisputeReleasePresentmentService(fresh).present_collection_dispute_release(
                TENANT_ONE, stored.collection_dispute_id
            )
        self.assertEqual(
            held_release.exception.rejection_reason_code,
            "collection_dispute_release_not_found",
        )

        self.assertEqual(self.ledger.insert_collection_dispute(stored), stored)
        self.assertEqual(
            self.ledger.insert_collection_dispute(
                replace(stored, collection_dispute_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dispute(replace(stored, currency_code="usd"))
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dispute(
                replace(stored, source_payload_hash="md5:abc")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dispute(
                replace(stored, collection_dispute_status="released")
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dispute(
                replace(
                    stored,
                    remaining_outstanding_amount=Decimal("-0.001"),
                    collection_dispute_id=uuid4(),
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.insert_collection_dispute(
                replace(
                    stored,
                    collection_dispute_id=later.collection_dispute_id,
                    collection_case_id=uuid4(),
                    source_payload_hash="sha256:" + "d" * 64,
                )
            )
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_dispute_released(uuid4(), released_at)
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_disputed(uuid4())
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_released_from_dispute(uuid4())
        settled_case = self.ledger.get_collection_case(collection.collection_case_id)
        assert settled_case is not None
        self.assertEqual(
            self.ledger.mark_collection_case_disputed(collection.collection_case_id),
            self.ledger.get_collection_case(collection.collection_case_id),
        )
        self.assertEqual(
            self.ledger.mark_collection_dispute_released(
                later.collection_dispute_id, released_at
            ).collection_dispute_status,
            "released",
        )
        self.assertEqual(
            self.ledger.mark_collection_case_released_from_dispute(
                afternoon_collection.collection_case_id
            ).collection_case_status,
            "open",
        )
        written_off = CollectionWriteOffService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 16, 30, tzinfo=UTC)
        ).write_off_collection_case(TENANT_ONE, afternoon_collection.collection_case_id)
        self.assertEqual(written_off.collection_write_off_outcome_code.value, "accepted")
        settled = CollectionCaseSettlementService(self.ledger).settle_collection_case(
            TENANT_ONE, afternoon_collection.collection_case_id
        )
        self.assertEqual(settled.collection_case_settlement_outcome_code.value, "accepted")
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_disputed(afternoon_collection.collection_case_id)
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_released_from_dispute(
                afternoon_collection.collection_case_id
            )
        with self.connection.transaction():
            self.connection.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = 'voided'
                WHERE collection_case_id = %s
                """,
                (afternoon_collection.collection_case_id,),
            )
        with self.assertRaises(ValueError):
            self.ledger.mark_collection_case_released_from_dispute(
                afternoon_collection.collection_case_id
            )
        dunning_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf12c",
            source_event_key="workflow_381:step_25:attempt_01",
            occurred_at="2026-08-16T23:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "3000",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(dunning_usage)
        dunning_window = TimeWindow.from_iso8601(
            "2026-08-16T23:00:00Z", "2026-08-17T00:00:00Z"
        )
        dunning_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, dunning_window, 1, rate_card_code="cwl_standard"
        )
        dunning_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, dunning_rating.rating_run_id
        )
        dunning_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, dunning_draft.invoice_draft_id)
        assert dunning_collection.collection_case_id is not None
        dunning_notice = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).record_dunning_event(
            TENANT_ONE, dunning_collection.collection_case_id, "first_notice"
        )
        self.assertEqual(dunning_notice.collection_case_outcome_code.value, "accepted")
        dunning_hold = CollectionDisputeService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
        ).hold_collection_case(TENANT_ONE, dunning_collection.collection_case_id)
        self.assertEqual(dunning_hold.collection_dispute_outcome_code.value, "accepted")
        assert dunning_hold.collection_dispute_id is not None
        dunning_release = CollectionDisputeReleaseService(
            self.ledger, clock=lambda: datetime(2026, 8, 18, 23, 30, tzinfo=UTC)
        ).release_collection_dispute(TENANT_ONE, dunning_hold.collection_dispute_id)
        self.assertEqual(dunning_release.collection_dispute_release_outcome_code.value, "accepted")
        self.assertEqual(dunning_release.collection_case_status, "dunning")
        self.assertEqual(
            self.ledger.get_collection_case(
                dunning_collection.collection_case_id
            ).collection_case_status,
            "dunning",
        )

        class BlindFindReleasedLedger(PostgresUsageLedger):
            """Force the insert race when the stored dispute is already released."""

            def find_collection_dispute(self, *args, **kwargs):
                return None

        released_mismatch = CollectionDisputeService(
            BlindFindReleasedLedger(self.connection), clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, dunning_collection.collection_case_id)
        self.assertEqual(released_mismatch.collection_dispute_outcome_code.value, "rejected")
        self.assertEqual(
            released_mismatch.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_DISPUTE_RELEASED,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            5,
        )

        race_usage = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf02c",
            source_event_key="workflow_381:step_24:attempt_01",
            occurred_at="2026-08-16T21:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "2500",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(self.ledger).ingest_usage_event(race_usage)
        race_window = TimeWindow.from_iso8601(
            "2026-08-16T21:00:00Z", "2026-08-16T22:00:00Z"
        )
        race_rating = UsageRatingService(self.ledger).rate_usage_window(
            TENANT_ONE, race_window, 1, rate_card_code="cwl_standard"
        )
        race_draft = InvoiceDraftService(self.ledger).draft_invoice(
            TENANT_ONE, race_rating.rating_run_id
        )
        race_collection = CollectionCaseService(
            self.ledger, clock=lambda: CATALOG_START
        ).open_collection_case(TENANT_ONE, race_draft.invoice_draft_id)
        assert race_collection.collection_case_id is not None
        race_case = self.ledger.get_collection_case(race_collection.collection_case_id)
        assert race_case is not None
        race_hash = compute_dispute_payload_hash(
            {
                "collection_case_id": str(race_case.collection_case_id),
                "invoice_draft_id": str(race_case.invoice_draft_id),
                "currency_code": race_case.currency_code,
                "remaining_outstanding_amount": format_exact_decimal(
                    race_case.outstanding_amount
                ),
                "collection_dispute_contract_version": 1,
            }
        )
        raced_row = self.ledger.insert_collection_dispute(
            StoredCollectionDispute(
                collection_dispute_id=uuid4(),
                tenant_account_id=tenant.tenant_account_id,
                collection_case_id=race_case.collection_case_id,
                invoice_draft_id=race_case.invoice_draft_id,
                issued_invoice_id=None,
                collection_dispute_contract_version=1,
                source_payload_hash=race_hash,
                currency_code=race_case.currency_code,
                remaining_outstanding_amount=race_case.outstanding_amount,
                collection_dispute_status="held",
                held_at=datetime(2026, 8, 18, 21, 0, tzinfo=UTC),
            )
        )
        self.assertEqual(
            self.ledger.get_collection_case(race_case.collection_case_id).collection_case_status,
            "open",
        )

        class BlindFindMissingCaseLedger(PostgresUsageLedger):
            """Force the insert race when the stored case cannot be loaded."""

            def __init__(self, connection: object) -> None:
                super().__init__(connection)
                self._hide_case = False

            def find_collection_dispute(self, *args, **kwargs):
                return None

            def insert_collection_dispute(self, dispute):
                stored_dispute = PostgresUsageLedger.insert_collection_dispute(self, dispute)
                self._hide_case = True
                return stored_dispute

            def get_collection_case(self, collection_case_id):
                if self._hide_case:
                    return None
                return PostgresUsageLedger.get_collection_case(self, collection_case_id)

        missing_case = CollectionDisputeService(
            BlindFindMissingCaseLedger(self.connection), clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, race_collection.collection_case_id)
        self.assertEqual(missing_case.collection_dispute_outcome_code.value, "rejected")
        self.assertEqual(
            missing_case.rejection_reason_code,
            CollectionDisputeRejectionReasonCode.COLLECTION_CASE_NOT_FOUND,
        )
        self.assertEqual(
            self.ledger.get_collection_case(race_case.collection_case_id).collection_case_status,
            "open",
        )

        class BlindFindLedger(PostgresUsageLedger):
            """Force the insert path used after a concurrent identity race."""

            def find_collection_dispute(self, *args, **kwargs):
                return None

        raced = CollectionDisputeService(
            BlindFindLedger(self.connection), clock=lambda: held_at
        ).hold_collection_case(TENANT_ONE, race_collection.collection_case_id)
        self.assertEqual(raced.collection_dispute_outcome_code.value, "duplicate_replay")
        self.assertEqual(raced.collection_dispute_id, raced_row.collection_dispute_id)
        self.assertEqual(
            self.ledger.get_collection_case(race_case.collection_case_id).collection_case_status,
            "disputed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM billing_core.collection_dispute"
            ).fetchone()[0],
            6,
        )

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
