"""Repository contract tests for the initial metering and billing foundation."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

from scripts.validate_repository import (
    main,
    find_mutable_action_references,
    find_placeholder_tokens,
    validate_accounting_journal_proposal,
    validate_repository,
    validate_schema_instance,
    validate_sql_object_names,
)


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    """Verify repository contracts without contacting external services."""

    def test_repository_contracts_are_valid(self) -> None:
        """The checked-in repository must satisfy every foundation invariant."""
        self.assertEqual(validate_repository(ROOT), ())

    def test_missing_required_file_is_reported(self) -> None:
        """A partial checkout must produce an actionable missing-file error."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            errors = validate_repository(Path(temporary_directory))
        self.assertIn("missing required file: README.md", errors)

    def test_usage_event_accepts_reported_usage(self) -> None:
        """Provider-reported usage with an idempotency key is contract-valid."""
        schema = self._schema("usage-event.schema.json")
        instance = {
            "event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
            "event_contract_version": 1,
            "source_event_key": "workflow_381:step_04:attempt_01",
            "source_payload_hash": "sha256:" + "d" * 64,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "billing_principal_reference": "urn:cwl:tenant_001:billing_principal:019d7002",
            "credential_reference": "urn:cwl:tenant_001:credential_record:019d7003",
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
        self.assertEqual(validate_schema_instance(schema, instance), ())

    def test_usage_ingestion_receipt_accepts_replay_counts(self) -> None:
        """A batch receipt records accepted, replayed, and rejected events."""
        schema = self._schema("usage-ingestion-receipt.schema.json")
        instance = {
            "batch_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "receipt_contract_version": 1,
            "accepted_event_count": 1,
            "duplicate_replay_count": 1,
            "rejected_event_count": 1,
            "event_receipts": [
                {
                    "source_event_key": "workflow_381:step_04:attempt_01",
                    "ingestion_outcome_code": "accepted",
                    "event_contract_version": 1,
                    "source_payload_hash": "sha256:" + "d" * 64,
                    "tenant_reference": "urn:cwl:tenant_001",
                    "usage_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
                },
                {
                    "source_event_key": "workflow_381:step_04:attempt_01",
                    "ingestion_outcome_code": "duplicate_replay",
                    "event_contract_version": 1,
                    "source_payload_hash": "sha256:" + "d" * 64,
                    "usage_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
                },
                {
                    "source_event_key": "unavailable_source_event_key",
                    "ingestion_outcome_code": "rejected",
                    "rejection_reason_code": "schema_invalid",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())

    def test_usage_ingestion_receipt_schema_requires_outcome_evidence(self) -> None:
        """Accepted and replay receipts need identity fields; rejected receipts need a reason."""
        schema = self._schema("usage-ingestion-receipt.schema.json")
        accepted = {
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
                }
            ],
        }
        self.assertTrue(validate_schema_instance(schema, accepted))
        rejected = {
            "batch_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf621",
            "receipt_contract_version": 1,
            "accepted_event_count": 0,
            "duplicate_replay_count": 0,
            "rejected_event_count": 1,
            "event_receipts": [
                {
                    "source_event_key": "unavailable_source_event_key",
                    "ingestion_outcome_code": "rejected",
                }
            ],
        }
        self.assertTrue(validate_schema_instance(schema, rejected))

    def test_usage_event_rejects_prompt_content(self) -> None:
        """Billing events must reject undeclared prompt or response content."""
        schema = self._schema("usage-event.schema.json")
        instance = {
            "event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61c",
            "event_contract_version": 1,
            "source_event_key": "workflow_381:step_04:attempt_01",
            "source_payload_hash": "sha256:" + "d" * 64,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "billing_principal_reference": "urn:cwl:tenant_001:billing_principal:019d7002",
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
            "prompt": "secret customer content",
        }
        self.assertIn(
            "$: additional property is not allowed: prompt",
            validate_schema_instance(schema, instance),
        )

    def test_provider_capability_keeps_roles_separate(self) -> None:
        """A provider advertises explicit capabilities instead of one giant type."""
        schema = self._schema("provider-capability.schema.json")
        instance = {
            "provider_code": "lemon_squeezy",
            "provider_roles": ["merchant_of_record"],
            "capabilities": [
                "hosted_checkout",
                "subscription_collection",
                "metered_usage_push",
                "settlement_export",
            ],
            "effective_from": "2026-08-16T00:00:00Z",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())

    def test_accounting_proposal_is_balanced_and_not_posted(self) -> None:
        """Billing may propose a balanced entry but cannot claim legal posting."""
        schema = self._schema("accounting-journal-proposal.schema.json")
        proposal = {
            "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61d",
            "proposal_contract_version": 1,
            "idempotency_key": "invoice_019d:issued:v1",
            "tenant_reference": "urn:cwl:tenant_001",
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
                    "debit_amount": "110000.25",
                    "credit_amount": "0",
                },
                {
                    "line_number": 2,
                    "account_role_code": "usage_revenue",
                    "debit_amount": "0",
                    "credit_amount": "100000.20",
                },
                {
                    "line_number": 3,
                    "account_role_code": "tax_payable",
                    "debit_amount": "0",
                    "credit_amount": "10000.05",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, proposal), ())
        self.assertEqual(validate_accounting_journal_proposal(schema, proposal), ())
        self.assertEqual(
            sum(Decimal(line["debit_amount"]) for line in proposal["lines"]),
            sum(Decimal(line["credit_amount"]) for line in proposal["lines"]),
        )
        invalid = dict(proposal, proposal_status="posted")
        self.assertIn(
            "$.proposal_status: value is not in the allowed enumeration",
            validate_accounting_journal_proposal(schema, invalid),
        )

    def test_accounting_domain_validation_rejects_imbalance_and_duplicate_lines(self) -> None:
        """Semantic accounting validation rejects totals or line identities that drift."""
        schema = self._schema("accounting-journal-proposal.schema.json")
        base = {
            "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61e",
            "proposal_contract_version": 1,
            "idempotency_key": "invoice_019d:cash:v1",
            "tenant_reference": "urn:cwl:tenant_001",
            "legal_entity_reference": "urn:cwl:entity_001",
            "intended_book_role_code": "primary_statutory",
            "transaction_currency": "KRW",
            "transaction_date": "2026-08-31",
            "accounting_date": "2026-08-31",
            "source_payload_hash": "sha256:" + "c" * 64,
            "proposed_at": "2026-08-31T23:59:59Z",
            "proposal_status": "validated",
            "source_event_references": ["urn:cwl:settlement:019d"],
            "lines": [
                {
                    "line_number": 1,
                    "account_role_code": "cash_clearing",
                    "debit_amount": "90.50",
                    "credit_amount": "0",
                },
                {
                    "line_number": 2,
                    "account_role_code": "provider_settlement_receivable",
                    "debit_amount": "0",
                    "credit_amount": "90.50",
                },
            ],
        }
        self.assertEqual(validate_accounting_journal_proposal(schema, base), ())

        unbalanced = json.loads(json.dumps(base))
        unbalanced["lines"][1]["credit_amount"] = "90.49"
        self.assertIn(
            "$: debit and credit totals must balance",
            validate_accounting_journal_proposal(schema, unbalanced),
        )

        duplicated = json.loads(json.dumps(base))
        duplicated["lines"][1]["line_number"] = 1
        self.assertIn(
            "$: journal line numbers must be unique",
            validate_accounting_journal_proposal(schema, duplicated),
        )

    def test_migration_enforces_tenant_scoped_references(self) -> None:
        """Attribution and usage foreign keys cannot cross tenant boundaries."""
        sql = (ROOT / "database/migrations/0001_initial_billing_core.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "UNIQUE (tenant_account_id, billing_account_id)",
            "UNIQUE (tenant_account_id, billing_principal_id)",
            "UNIQUE (tenant_account_id, credential_record_id)",
            "FOREIGN KEY (tenant_account_id, credential_record_id)",
            "FOREIGN KEY (tenant_account_id, billing_principal_id)",
            "FOREIGN KEY (tenant_account_id, billing_account_id)",
            "FOREIGN KEY (meter_definition_id, quality_code)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_usage_idempotency_migration_binds_hash_and_contract_version(self) -> None:
        """The follow-up migration must keep hash-version identity tenant-scoped."""
        sql = (ROOT / "database/migrations/0002_usage_event_idempotency.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "event_contract_version",
            "producer_event_id",
            "SET event_contract_version = 1",
            "UNIQUE (tenant_account_id, event_payload_hash, event_contract_version)",
            "UNIQUE (tenant_account_id, usage_event_id)",
            "UNIQUE (tenant_account_id, producer_event_id)",
            "CREATE TABLE billing_core.usage_ingestion_receipt",
            "FOREIGN KEY (tenant_account_id, usage_event_id)",
            "ingestion_outcome_code",
        ):
            self.assertIn(expected_fragment, sql)

    def test_rating_run_accepts_exact_invoice_intent_totals(self) -> None:
        """A rating-run contract records exact decimal invoice-intent lines."""
        schema = self._schema("rating-run.schema.json")
        instance = {
            "rating_contract_version": 1,
            "rating_outcome_code": "accepted",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tenant_reference": "urn:cwl:tenant_001",
            "rate_card_code": "cwl_standard",
            "rate_card_version": 1,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "usage_snapshot_hash": "sha256:" + "d" * 64,
            "currency_code": "USD",
            "rated_total_amount": "0.003705",
            "rating_lines": [
                {
                    "line_number": 1,
                    "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                    "meter_code": "gen_ai_output_token",
                    "unit_code": "token",
                    "rated_quantity": "1852.5",
                    "unit_price_amount": "0.000002",
                    "line_total_amount": "0.003705",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())

    def test_rating_run_schema_rejects_posted_journal_and_open_properties(self) -> None:
        """Rating remains invoice-intent only and cannot claim statutory posting."""
        schema = self._schema("rating-run.schema.json")
        instance = {
            "rating_contract_version": 1,
            "rating_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
            "proposal_status": "posted",
        }
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, instance),
        )

    def test_invoice_draft_accepts_exact_draft_totals(self) -> None:
        """An invoice-draft contract records exact decimal draft-only lines."""
        schema = self._schema("invoice-draft.schema.json")
        instance = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "accepted",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tenant_reference": "urn:cwl:tenant_001",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf621",
            "usage_snapshot_hash": "sha256:" + "d" * 64,
            "currency_code": "USD",
            "invoice_draft_status": "draft",
            "drafted_total_amount": "0.003705",
            "invoice_draft_lines": [
                {
                    "line_number": 1,
                    "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                    "meter_code": "gen_ai_output_token",
                    "unit_code": "token",
                    "rated_quantity": "1852.5",
                    "unit_price_amount": "0.000002",
                    "line_total_amount": "0.003705",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())

    def test_invoice_draft_schema_rejects_posted_and_issued_status(self) -> None:
        """An invoice draft cannot claim issued, collected, or posted status."""
        schema = self._schema("invoice-draft.schema.json")
        instance = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "rejected",
            "rejection_reason_code": "rating_run_not_found",
            "proposal_status": "posted",
        }
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, instance),
        )
        issued = {
            "invoice_draft_contract_version": 1,
            "invoice_draft_outcome_code": "accepted",
            "invoice_draft_status": "issued",
        }
        self.assertIn(
            "$.invoice_draft_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, issued),
        )

    def test_invoice_draft_migration_persists_append_only_drafts(self) -> None:
        """The invoice-draft migration must keep identity tenant-scoped and draft-only."""
        sql = (ROOT / "database/migrations/0004_invoice_draft.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.invoice_draft",
            "CREATE TABLE billing_core.invoice_draft_line",
            "UNIQUE (tenant_account_id, rating_run_id)",
            "UNIQUE (tenant_account_id, invoice_draft_id)",
            "FOREIGN KEY (tenant_account_id, rating_run_id)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "invoice_draft_status text NOT NULL CHECK (invoice_draft_status IN ('draft'))",
            "drafted_total_amount numeric(38, 12)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_journal_proposal_migration_persists_append_only_balanced_lines(self) -> None:
        """The journal-proposal migration must stay tenant-scoped and proposal-only."""
        sql = (ROOT / "database/migrations/0005_journal_proposal.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.journal_proposal",
            "CREATE TABLE billing_core.journal_proposal_line",
            "UNIQUE (tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)",
            "UNIQUE (tenant_account_id, journal_proposal_id)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "FOREIGN KEY (tenant_account_id, journal_proposal_id)",
            "proposal_status text NOT NULL CHECK (proposal_status IN ('draft', 'validated', 'exported', 'rejected'))",
            "UNIQUE (journal_proposal_id, line_number)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_collection_case_accepts_open_and_dunning_status(self) -> None:
        """A collection-case contract records exact outstanding and commercial status only."""
        schema = self._schema("collection-case.schema.json")
        instance = {
            "collection_case_contract_version": 1,
            "collection_case_outcome_code": "accepted",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "collection_case_status": "dunning",
            "outstanding_amount": "0.003705",
            "dunning_events": [
                {
                    "dunning_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf631",
                    "dunning_event_number": 1,
                    "dunning_notice_code": "first_notice",
                    "occurred_at": "2026-08-18T09:00:00Z",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        paid = dict(instance, collection_case_status="paid")
        self.assertIn(
            "$.collection_case_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, paid),
        )
        settled = dict(instance, collection_case_status="settled", outstanding_amount="0")
        self.assertEqual(validate_schema_instance(schema, settled), ())

    def test_collection_case_migration_persists_append_only_cases_and_notices(self) -> None:
        """The collection-case migration must stay tenant-scoped and commercial-only."""
        sql = (ROOT / "database/migrations/0006_collection_case.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.collection_case",
            "CREATE TABLE billing_core.collection_dunning_event",
            "UNIQUE (tenant_account_id, invoice_draft_id)",
            "UNIQUE (tenant_account_id, collection_case_id)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "FOREIGN KEY (tenant_account_id, collection_case_id)",
            "collection_case_status text NOT NULL CHECK (collection_case_status IN ('open', 'dunning'))",
            "UNIQUE (collection_case_id, dunning_notice_code)",
            "UNIQUE (collection_case_id, dunning_event_number)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_payment_intent_accepts_projected_status_only(self) -> None:
        """A payment-intent contract records exact amounts and projected-only status."""
        schema = self._schema("payment-intent.schema.json")
        instance = {
            "payment_intent_contract_version": 1,
            "payment_intent_outcome_code": "accepted",
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_intent_status": "projected",
            "payment_amount": "0.003705",
            "source_payload_hash": "sha256:" + "1" * 64,
            "projected_at": "2026-08-17T19:30:00Z",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        captured = dict(instance, payment_intent_status="captured")
        self.assertIn(
            "$.payment_intent_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )

    def test_payment_intent_migration_persists_append_only_projected_intents(self) -> None:
        """The payment-intent migration must stay tenant-scoped and capture-free."""
        sql = (ROOT / "database/migrations/0007_payment_intent.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.payment_intent",
            "UNIQUE (tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)",
            "UNIQUE (tenant_account_id, payment_intent_id)",
            "FOREIGN KEY (tenant_account_id, collection_case_id)",
            "payment_intent_status text NOT NULL CHECK (payment_intent_status IN ('projected', 'cancelled', 'rejected'))",
            "payment_amount numeric(38, 12)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_payment_receipt_accepts_applied_status_only(self) -> None:
        """A payment-receipt contract records exact amounts and applied-only status."""
        schema = self._schema("payment-receipt.schema.json")
        instance = {
            "settlement_contract_version": 1,
            "payment_settlement_outcome_code": "accepted",
            "payment_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf650",
            "tenant_reference": "urn:cwl:tenant_001",
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_receipt_status": "applied",
            "received_amount": "0.003705",
            "remaining_outstanding_amount": "0",
            "collection_case_status": "settled",
            "source_payload_hash": "sha256:" + "1" * 64,
            "received_at": "2026-08-17T20:15:00Z",
            "next_operator_action": "Emit a cash journal proposal to AIS.",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        captured = dict(instance, payment_receipt_status="captured")
        self.assertIn(
            "$.payment_receipt_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )

    def test_payment_receipt_migration_persists_append_only_applied_receipts(self) -> None:
        """The payment-receipt migration must stay tenant-scoped and capture-free."""
        sql = (ROOT / "database/migrations/0008_payment_receipt.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.payment_receipt",
            "UNIQUE (tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)",
            "UNIQUE (tenant_account_id, payment_receipt_id)",
            "FOREIGN KEY (tenant_account_id, payment_intent_id)",
            "FOREIGN KEY (tenant_account_id, collection_case_id)",
            "payment_receipt_status text NOT NULL CHECK (payment_receipt_status IN ('applied'))",
            "received_amount numeric(38, 12)",
            "CHECK (collection_case_status IN ('open', 'dunning', 'settled'))",
            "CHECK (outstanding_amount >= 0)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_cash_journal_migration_reuses_journal_proposal_for_receipts(self) -> None:
        """Cash proposals reuse journal_proposal and add a receipt-scoped identity."""
        sql = (ROOT / "database/migrations/0009_cash_journal_proposal.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN payment_receipt_id uuid",
            "FOREIGN KEY (tenant_account_id, payment_receipt_id)",
            "REFERENCES billing_core.payment_receipt (tenant_account_id, payment_receipt_id)",
            "CREATE UNIQUE INDEX journal_proposal_receipt_identity",
            "payment_receipt_id IS NOT NULL",
        ):
            self.assertIn(expected_fragment, sql)

    def test_credit_adjustment_accepts_recorded_status_and_closed_reasons(self) -> None:
        """A credit-adjustment contract records exact amounts and closed reasons."""
        schema = self._schema("credit-adjustment.schema.json")
        instance = {
            "credit_adjustment_contract_version": 1,
            "credit_adjustment_outcome_code": "accepted",
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "credit_adjustment_status": "recorded",
            "credit_reason_code": "rating_correction",
            "credit_amount": "0.001000",
            "tax_exclusive_amount": "0.001000",
            "tax_amount": "0",
            "remaining_adjustable_amount": "0.002705",
            "remaining_outstanding_amount": "0.002705",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "collection_case_status": "open",
            "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf670",
            "proposal_status": "validated",
            "source_payload_hash": "sha256:" + "2" * 64,
            "idempotency_key": "urn:cwl:tenant_001:credit_adjustment:019d7b92-1aa0-7a7f-b61c-962c0f4bf660:sha256:"
            + "2" * 64
            + ":v1",
            "recorded_at": "2026-08-17T20:15:00Z",
            "next_operator_action": "Let AIS pull the validated credit journal proposal.",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        posted = dict(instance, proposal_status="posted")
        self.assertIn(
            "$.proposal_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, posted),
        )
        unknown_reason = dict(instance, credit_reason_code="tax_refund")
        self.assertIn(
            "$.credit_reason_code: value is not in the allowed enumeration",
            validate_schema_instance(schema, unknown_reason),
        )

    def test_credit_adjustment_migration_reuses_journal_proposal_for_credits(self) -> None:
        """Credit rows stay tenant-scoped and reuse journal_proposal identity."""
        sql = (ROOT / "database/migrations/0011_credit_adjustment.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.credit_adjustment",
            "UNIQUE (tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)",
            "UNIQUE (tenant_account_id, credit_adjustment_id)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "credit_reason_code text NOT NULL CHECK (credit_reason_code IN ('rating_correction', 'goodwill', 'billing_error'))",
            "credit_amount numeric(38, 12) NOT NULL CHECK (credit_amount > 0)",
            "ADD COLUMN credit_adjustment_id uuid",
            "CREATE UNIQUE INDEX journal_proposal_credit_identity",
            "credit_adjustment_id IS NOT NULL",
        ):
            self.assertIn(expected_fragment, sql)

    def test_credit_tax_unwind_migration_stores_split_on_credit_adjustment(self) -> None:
        """Tax unwind columns stay on credit_adjustment and must sum to the credit."""
        sql = (ROOT / "database/migrations/0014_credit_tax_unwind.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN tax_exclusive_amount numeric(38, 12) NOT NULL DEFAULT 0",
            "ADD COLUMN tax_amount numeric(38, 12) NOT NULL DEFAULT 0",
            "CHECK (tax_exclusive_amount + tax_amount = credit_amount)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_tax_rate_accepts_published_version_and_closed_codes(self) -> None:
        """A tax-rate contract records an exact rate in [0, 1] and closed codes."""
        schema = self._schema("tax-rate.schema.json")
        instance = {
            "tax_rate_contract_version": 1,
            "tax_rate_outcome_code": "accepted",
            "tax_rate_schedule_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf690",
            "tax_rate_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf691",
            "tenant_reference": "urn:cwl:tenant_001",
            "tax_code": "vat",
            "tax_rate_version": 1,
            "tax_rate": "0.10",
            "source_payload_hash": "sha256:" + "4" * 64,
            "published_at": "2026-08-17T21:00:00Z",
            "next_operator_action": (
                "Publish a tax rate, assess the draft, then propose the journal and let AIS pull."
            ),
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        floated = dict(instance, tax_rate=0.10)
        self.assertTrue(validate_schema_instance(schema, floated))
        unknown_code = dict(instance, tax_code="excise")
        self.assertIn(
            "$.tax_code: value is not in the allowed enumeration",
            validate_schema_instance(schema, unknown_code),
        )

    def test_tax_assessment_accepts_inclusive_identity_and_closed_reasons(self) -> None:
        """A tax-assessment contract records exclusive, tax, and inclusive amounts."""
        schema = self._schema("tax-assessment.schema.json")
        instance = {
            "tax_assessment_contract_version": 1,
            "tax_assessment_outcome_code": "accepted",
            "tax_assessment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf692",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tax_rate_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf691",
            "tax_rate_version": 1,
            "tax_code": "vat",
            "tax_rate": "0.10",
            "currency_code": "USD",
            "tax_exclusive_amount": "100.00",
            "tax_amount": "10.00",
            "tax_inclusive_amount": "110.00",
            "source_payload_hash": "sha256:" + "5" * 64,
            "assessed_at": "2026-08-17T21:05:00Z",
            "next_operator_action": (
                "Publish a tax rate, assess the draft, then propose the journal and let AIS pull."
            ),
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        unknown_reason = {
            "tax_assessment_contract_version": 1,
            "tax_assessment_outcome_code": "rejected",
            "rejection_reason_code": "tax_exempt",
        }
        self.assertIn(
            "$.rejection_reason_code: value is not in the allowed enumeration",
            validate_schema_instance(schema, unknown_reason),
        )

    def test_tax_assessment_migration_is_tenant_scoped_and_append_only(self) -> None:
        """Tax schedules, versions, and assessments stay tenant-scoped."""
        sql = (ROOT / "database/migrations/0013_tax_assessment.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.tax_rate_schedule",
            "CREATE TABLE billing_core.tax_rate_version",
            "CREATE TABLE billing_core.tax_assessment",
            "UNIQUE (tenant_account_id, tax_code)",
            "UNIQUE (tenant_account_id, tax_rate_schedule_id, version_number)",
            "UNIQUE (tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)",
            "UNIQUE (tenant_account_id, invoice_draft_id)",
            "UNIQUE (tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version)",
            "tax_rate numeric(38, 12) NOT NULL CHECK (tax_rate >= 0 AND tax_rate <= 1)",
            "CHECK (tax_inclusive_amount = tax_exclusive_amount + tax_amount)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "FOREIGN KEY (tenant_account_id, tax_rate_version_id)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_rate_card_accepts_published_version_and_closed_reasons(self) -> None:
        """A rate-card contract records exact unit amounts and closed outcomes."""
        schema = self._schema("rate-card.schema.json")
        instance = {
            "rate_card_contract_version": 1,
            "rate_card_outcome_code": "accepted",
            "rate_card_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "rate_card_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "tenant_reference": "urn:cwl:tenant_001",
            "rate_card_name": "cwl_standard",
            "rate_card_version": 1,
            "currency_code": "USD",
            "source_payload_hash": "sha256:" + "3" * 64,
            "published_at": "2026-08-17T20:30:00Z",
            "next_operator_action": "Publish a rate card, then rate a window against that version.",
            "lines": [
                {
                    "metric_code": "gen_ai_output_token",
                    "unit_amount": "0.000002",
                    "currency_code": "USD",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        floated = dict(instance, lines=[{"metric_code": "gen_ai_output_token", "unit_amount": 0.000002, "currency_code": "USD"}])
        self.assertTrue(validate_schema_instance(schema, floated))
        unknown_reason = {
            "rate_card_contract_version": 1,
            "rate_card_outcome_code": "rejected",
            "rejection_reason_code": "tax_exempt",
        }
        self.assertIn(
            "$.rejection_reason_code: value is not in the allowed enumeration",
            validate_schema_instance(schema, unknown_reason),
        )

    def test_rate_card_catalog_migration_is_tenant_scoped_and_append_only(self) -> None:
        """Catalog versions stay tenant-scoped and never reuse a single-word table."""
        sql = (ROOT / "database/migrations/0012_rate_card_catalog.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN tenant_account_id uuid",
            "ADD COLUMN rate_card_name text",
            "CREATE TABLE billing_core.rate_card_version",
            "CREATE TABLE billing_core.rate_card_line",
            "UNIQUE (tenant_account_id, rate_card_id, version_number)",
            "UNIQUE (tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)",
            "UNIQUE (tenant_account_id, rate_card_version_id)",
            "UNIQUE (tenant_account_id, rate_card_version_id, metric_code)",
            "unit_amount numeric(38, 12) NOT NULL CHECK (unit_amount > 0)",
            "FOREIGN KEY (tenant_account_id, rate_card_id)",
            "FOREIGN KEY (tenant_account_id, rate_card_version_id)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_posting_receipt_observation_migration_is_tenant_scoped_and_append_only(self) -> None:
        """The observation table must not use AIS receipt_id as the primary key."""
        sql = (ROOT / "database/migrations/0010_posting_receipt_observation.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.posting_receipt_observation",
            "posting_receipt_observation_id uuid PRIMARY KEY",
            "UNIQUE (tenant_account_id, idempotency_key)",
            "UNIQUE (tenant_account_id, receipt_id)",
            "UNIQUE (tenant_account_id, posting_receipt_observation_id)",
            "CHECK (posting_status_code IN ('posted', 'held', 'rejected', 'reversed'))",
            "CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')",
            "FOREIGN KEY (tenant_account_id)",
        ):
            self.assertIn(expected_fragment, sql)
        self.assertNotIn("receipt_id uuid PRIMARY KEY", sql)

    def test_rating_migration_persists_append_only_runs_and_lines(self) -> None:
        """The rating migration must keep run identity tenant-scoped and append-only."""
        sql = (ROOT / "database/migrations/0003_rating_run.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.rate_card",
            "CREATE TABLE billing_core.rate_card_price",
            "CREATE TABLE billing_core.rating_run",
            "CREATE TABLE billing_core.rating_line",
            "UNIQUE (rate_card_code, rate_card_version)",
            "UNIQUE (tenant_account_id, window_started_at, window_ended_at, rate_card_id, usage_snapshot_hash)",
            "UNIQUE (tenant_account_id, rating_run_id)",
            "FOREIGN KEY (tenant_account_id, rating_run_id)",
            "FOREIGN KEY (tenant_account_id, billing_account_id)",
            "unit_price_amount numeric(38, 12)",
            "rated_total_amount numeric(38, 12)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_schema_validator_reports_required_type_and_reference_errors(self) -> None:
        """The offline validator covers required, type, reference, and one-of rules."""
        schema = self._schema("accounting-journal-proposal.schema.json")
        invalid = {
            "proposal_id": 7,
            "proposal_contract_version": 1,
            "idempotency_key": "invoice_019d:issued:v1",
            "tenant_reference": "urn:cwl:tenant_001",
            "legal_entity_reference": "urn:cwl:entity_001",
            "intended_book_role_code": "primary_statutory",
            "transaction_currency": "KRW",
            "transaction_date": "2026-08-31",
            "accounting_date": "2026-08-31",
            "source_payload_hash": "sha256:" + "b" * 64,
            "proposed_at": "2026-08-31T23:59:59Z",
            "proposal_status": "draft",
            "source_event_references": ["urn:cwl:invoice:019d"],
            "lines": [
                {
                    "line_number": 1,
                    "account_role_code": "accounts_receivable",
                    "debit_amount": "0",
                    "credit_amount": "0",
                },
                {
                    "line_number": 2,
                    "account_role_code": "usage_revenue",
                    "debit_amount": "0",
                    "credit_amount": "1",
                },
            ],
        }
        errors = validate_schema_instance(schema, invalid)
        self.assertIn("$.proposal_id: expected string", errors)
        self.assertIn("$.lines[0]: expected exactly one oneOf branch to match", errors)

    def test_repository_reports_integrated_supply_chain_and_sql_violations(self) -> None:
        """The aggregate validator reports provider IDs, placeholders, and mutable actions."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_root = Path(temporary_directory) / "repository"
            shutil.copytree(ROOT, copied_root)
            migration = copied_root / "database/migrations/0001_initial_billing_core.sql"
            migration.write_text(
                migration.read_text(encoding="utf-8")
                + "\n-- provider_customer_id must not be placed in the core.\n",
                encoding="utf-8",
            )
            readme = copied_root / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8") + "\nTO" + "DO: unresolved.\n",
                encoding="utf-8",
            )
            requirements = copied_root / "requirements-quality.txt"
            requirements.write_text("coverage==7.15.4\n", encoding="utf-8")
            workflow = copied_root / ".github/workflows/ci.yml"
            workflow.write_text(
                workflow.read_text(encoding="utf-8")
                + "\n# invalid fixture\n# uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            second_migration = copied_root / "database/migrations/0002_usage_event_idempotency.sql"
            second_migration.write_text(
                second_migration.read_text(encoding="utf-8")
                + "\n-- stripe_customer_id must not be placed in the core.\n",
                encoding="utf-8",
            )
            errors = validate_repository(copied_root)
        self.assertIn(
            "database/migrations/0001_initial_billing_core.sql: provider-specific identifiers must remain in mapping tables",
            errors,
        )
        self.assertIn(
            "database/migrations/0002_usage_event_idempotency.sql: provider-specific identifiers must remain in mapping tables",
            errors,
        )
        self.assertIn("quality dependencies must be hash locked", errors)
        self.assertIn("unresolved placeholder in README.md: TODO", errors)
        self.assertIn(
            "mutable GitHub Action reference in .github/workflows/ci.yml: actions/checkout@v4",
            errors,
        )

    def test_repository_reports_schema_metadata_and_json_errors(self) -> None:
        """Malformed, duplicated, and open schema roots are rejected deterministically."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schemas = root / "schemas"
            schemas.mkdir()
            (schemas / "00-invalid.schema.json").write_text("{", encoding="utf-8")
            malformed = {
                "$schema": "https://example.invalid/draft",
                "$id": "http://schemas.invalid/open",
                "type": "array",
                "additionalProperties": True,
            }
            (schemas / "01-malformed.schema.json").write_text(
                json.dumps(malformed), encoding="utf-8"
            )
            valid = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.example.test/duplicate",
                "type": "object",
                "additionalProperties": False,
            }
            for name in ("02-first.schema.json", "03-second.schema.json"):
                (schemas / name).write_text(json.dumps(valid), encoding="utf-8")
            errors = validate_repository(root)
        self.assertIn("invalid JSON in 00-invalid.schema.json", "\n".join(errors))
        self.assertIn("schema must declare Draft 2020-12: 01-malformed.schema.json", errors)
        self.assertIn("schema must have an HTTPS $id: 01-malformed.schema.json", errors)
        self.assertIn("schema root must be an object: 01-malformed.schema.json", errors)
        self.assertIn(
            "schema root must reject additional properties: 01-malformed.schema.json",
            errors,
        )
        self.assertIn(
            "duplicate schema $id: https://schemas.example.test/duplicate", errors
        )

    def test_offline_schema_validator_covers_scalar_array_and_object_constraints(self) -> None:
        """Every supported keyword emits stable diagnostics for invalid values."""
        string_schema = {
            "type": "string",
            "minLength": 2,
            "maxLength": 3,
            "pattern": "^a",
        }
        self.assertEqual(
            validate_schema_instance(string_schema, ""),
            (
                "$: string is shorter than minLength",
                "$: string does not match the required pattern",
            ),
        )
        self.assertEqual(
            validate_schema_instance(string_schema, "abcd"),
            ("$: string is longer than maxLength",),
        )

        array_schema = {
            "type": "array",
            "minItems": 2,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        }
        self.assertEqual(
            validate_schema_instance(array_schema, []),
            ("$: array has fewer than minItems",),
        )
        errors = validate_schema_instance(array_schema, [0, 0, 0, 0])
        self.assertIn("$: array has more than maxItems", errors)
        self.assertIn("$: array items must be unique", errors)
        self.assertIn("$[0]: integer is below the minimum", errors)
        self.assertEqual(
            validate_schema_instance({"type": "array", "items": True}, [1]), ()
        )

        object_schema = {
            "type": "object",
            "required": ["known_value"],
            "properties": {"known_value": {"type": "string"}},
            "additionalProperties": False,
        }
        self.assertEqual(
            validate_schema_instance(object_schema, {}),
            ("$: required property is missing: known_value",),
        )

    def test_offline_schema_validator_covers_formats_types_and_references(self) -> None:
        """Formats, unsupported types, and malformed local references fail closed."""
        for format_name, value in (
            ("uuid", "not-a-uuid"),
            ("date", "2026-13-40"),
            ("date-time", "2026-08-16T10:00:00"),
            ("unknown", "value"),
        ):
            self.assertTrue(
                validate_schema_instance(
                    {"type": "string", "format": format_name}, value
                )
            )
        with self.assertRaisesRegex(ValueError, "unsupported schema type"):
            validate_schema_instance({"type": "number"}, 1.5)
        with self.assertRaisesRegex(ValueError, "only local JSON Pointer"):
            validate_schema_instance({"$ref": "https://example.test/schema"}, {})
        with self.assertRaisesRegex(ValueError, "does not resolve to a schema object"):
            validate_schema_instance(
                {"$ref": "#/$defs/value", "$defs": {"value": "scalar"}}, {}
            )
        escaped_reference_schema = {
            "$ref": "#/$defs/a~1b~0c",
            "$defs": {"a/b~c": {"type": "string", "const": "ok"}},
        }
        self.assertEqual(
            validate_schema_instance(escaped_reference_schema, "ok"), ()
        )
        self.assertIn(
            "$: value does not equal the required constant",
            validate_schema_instance(escaped_reference_schema, "not-ok"),
        )
        self.assertEqual(
            validate_schema_instance({"type": "integer"}, True),
            ("$: expected integer",),
        )
        self.assertEqual(validate_schema_instance({}, None), ())

    def test_main_returns_process_status_for_valid_and_invalid_roots(self) -> None:
        """The command entrypoint prints diagnostics and returns a process status."""
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(ROOT)]), 0)
        self.assertIn("repository contracts valid", output.getvalue())

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([temporary_directory]), 1)
        self.assertIn("missing required file: README.md", output.getvalue())

        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["validate_repository", str(ROOT)]):
            with redirect_stdout(output):
                self.assertEqual(main(None), 0)

        output = io.StringIO()
        with mock.patch("scripts.validate_repository.Path.cwd", return_value=ROOT):
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)

    def test_mutable_action_reference_is_rejected(self) -> None:
        """Mutable action tags cannot produce exact-head supply-chain evidence."""
        self.assertEqual(
            find_mutable_action_references("- uses: actions/checkout@v4\n"),
            ("actions/checkout@v4",),
        )

    def test_single_word_database_object_is_rejected(self) -> None:
        """Every schema and table identifier must contain at least two words."""
        sql = """
        CREATE SCHEMA finance;
        CREATE TABLE finance.invoice (
            invoice_id uuid,
            status text
        );
        """
        self.assertEqual(
            validate_sql_object_names(sql),
            (
                "schema name must contain at least two snake_case words: finance",
                "table name must contain at least two snake_case words: invoice",
                "column name must contain at least two snake_case words: status",
            ),
        )

    def test_alter_table_add_column_is_checked_for_two_word_names(self) -> None:
        """ADD COLUMN identifiers must use two-or-more-word snake_case names."""
        self.assertEqual(
            validate_sql_object_names("ALTER TABLE usage_event ADD COLUMN status text;\n"),
            ("column name must contain at least two snake_case words: status",),
        )

    def test_repository_prefixes_sql_name_errors_with_migration_path(self) -> None:
        """SQL naming failures name the migration that introduced them."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_root = Path(temporary_directory) / "repository"
            shutil.copytree(ROOT, copied_root)
            (
                copied_root / "database/migrations/0002_usage_event_idempotency.sql"
            ).write_text(
                "ALTER TABLE usage_event ADD COLUMN status text;\n",
                encoding="utf-8",
            )
            errors = validate_repository(copied_root)
        self.assertTrue(
            any(
                error
                == (
                    "database/migrations/0002_usage_event_idempotency.sql: "
                    "column name must contain at least two snake_case words: status"
                )
                for error in errors
            )
        )

    def test_placeholder_tokens_are_rejected(self) -> None:
        """Accepted architecture documents cannot retain implementation placeholders."""
        self.assertEqual(
            find_placeholder_tokens("Complete text.\nTODO: decide later.\n"),
            ("TODO",),
        )

    def _schema(self, schema_name: str) -> dict[str, object]:
        return json.loads((ROOT / "schemas" / schema_name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
