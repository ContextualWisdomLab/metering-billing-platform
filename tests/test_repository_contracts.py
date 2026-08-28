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

from metering_billing.contracts import (
    validate_ais_outbox_drain,
    validate_account_statement_presentment,
    validate_rated_spend_presentment,
    validate_collection_aging_presentment,
    validate_collection_case_presentment,
    validate_payment_intent_presentment,
    validate_payment_receipt_presentment,
    validate_credit_adjustment_presentment,
    validate_spend_budget,
    validate_spend_budget_over_signal,
    validate_spend_budget_over_signal_presentment,
    validate_spend_budget_approaching_signal,
    validate_spend_budget_approaching_signal_presentment,
    validate_spend_budget_presentment,
    validate_spend_budget_evaluation_presentment,
    validate_billing_account_budget_status_presentment,
    validate_rate_card_presentment,
    validate_usage_event_presentment,
    validate_rating_run_presentment,
    validate_tax_assessment_presentment,
    validate_posting_receipt_observation_presentment,
    validate_webhook_delivery_presentment,
    validate_tenant_api_credential,
    validate_tenant_api_credential_presentment,
    validate_webhook_subscription_presentment,
    validate_dunning_event_presentment,
    validate_webhook_outbox_event_presentment,
    validate_issued_invoice,
    validate_issued_invoice_presentment,
    validate_issued_invoice_void,
    validate_issued_invoice_void_presentment,
    validate_issued_credit_note,
    validate_issued_credit_note_presentment,
    validate_issued_credit_note_void,
    validate_issued_credit_note_void_presentment,
    validate_webhook_delivery,
    validate_webhook_subscription,
)
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

    def test_boolean_json_schema_nodes_are_supported(self) -> None:
        """Draft 2020-12 boolean schemas must accept true and reject false."""
        schema = {
            "type": "object",
            "properties": {"accepted_value": True, "forbidden_value": False},
            "additionalProperties": False,
        }
        self.assertEqual(validate_schema_instance(schema, {"accepted_value": "ok"}), ())
        self.assertEqual(
            validate_schema_instance(schema, {"forbidden_value": "blocked"}),
            ("$.forbidden_value: schema is false",),
        )
        self.assertEqual(validate_schema_instance({"type": "array"}, []), ())
        self.assertEqual(
            validate_schema_instance({"type": "array", "items": False}, ["blocked"]),
            ("$[0]: schema is false",),
        )

    def test_reusable_workflow_action_refs_are_pinned(self) -> None:
        """Reusable workflow paths must obey the same immutable-ref policy."""
        mutable = "uses: ContextualWisdomLab/.github/.github/workflows/reusable.yml@main"
        pinned = (
            "uses: ContextualWisdomLab/.github/.github/workflows/reusable.yml@"
            + "a" * 40
        )
        self.assertEqual(
            find_mutable_action_references(mutable),
            ("ContextualWisdomLab/.github/.github/workflows/reusable.yml@main",),
        )
        self.assertEqual(find_mutable_action_references(pinned), ())

    def test_node_modules_placeholders_are_ignored(self) -> None:
        """Storybook install trees must not fail repository contract scans."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested = root / "operator_console" / "node_modules" / "semver"
            nested.mkdir(parents=True)
            (nested / "README.md").write_text("TODO leftover note\n", encoding="utf-8")
            errors = validate_repository(root)
        self.assertFalse(any("node_modules" in error for error in errors))
        self.assertFalse(any("unresolved placeholder" in error for error in errors))

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

    def test_persistence_integrity_migration_protects_references_and_intervals(self) -> None:
        """Database constraints preserve proposal identity and half-open assignments."""
        sql = (
            ROOT / "database/migrations/0036_persistence_integrity_constraints.sql"
        ).read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE EXTENSION IF NOT EXISTS btree_gist",
            "accounting_export_record_tenant_proposal_reference_key",
            "UNIQUE (tenant_account_id, proposal_reference)",
            "credential_assignment_no_overlap",
            "tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')",
        ):
            self.assertIn(expected_fragment, sql)

    def test_catalog_reference_migration_preserves_resolver_identity(self) -> None:
        """Catalog rows must retain the URNs used by the in-memory resolver."""
        sql = (ROOT / "database/migrations/0037_catalog_reference_identity.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN tenant_reference text",
            "SET tenant_reference = 'urn:cwl:' || tenant_account_code",
            "tenant_account_tenant_reference_key",
            "ADD COLUMN credential_reference text",
            "credential_record_id::text",
            "credential_record_tenant_reference_key",
            "ALTER COLUMN credential_reference SET NOT NULL",
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

    def test_usage_event_contract_metadata_migration_is_append_only(self) -> None:
        """The event table persists producer, trace, availability, and correction metadata."""
        sql = (ROOT / "database/migrations/0042_usage_event_contract_metadata.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN producer_contract_version integer NOT NULL DEFAULT 1",
            "ADD COLUMN repository_reference text",
            "ADD COLUMN trace_reference text",
            "ADD COLUMN correlation_reference text",
            "ADD COLUMN causation_reference text",
            "ADD COLUMN available_at timestamptz",
            "ADD COLUMN correction_lineage jsonb",
            "usage_event_producer_contract_version_positive",
            "usage_event_correction_lineage_object",
        ):
            self.assertIn(expected_fragment, sql)

    def test_correction_uuid_follow_up_migration_matches_schema_uuid_validation(self) -> None:
        """The follow-up keeps schema-valid non-RFC4122 UUID variants insertable."""
        sql = (
            ROOT
            / "database/migrations/0043_align_correction_uuid_validation.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("DROP CONSTRAINT usage_event_correction_lineage_object", sql)
        self.assertIn("ADD CONSTRAINT usage_event_correction_lineage_object", sql)
        self.assertIn(
            "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            sql,
        )

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

    def test_invoice_presentment_accepts_statement_totals_and_rejects_posted(self) -> None:
        """A presentment contract records exact due amounts and cannot claim posting."""
        from metering_billing.contracts import validate_invoice_presentment

        schema = self._schema("invoice-draft-presentment.schema.json")
        instance = {
            "invoice_presentment_contract_version": 1,
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "tenant_reference": "urn:cwl:tenant_001",
            "currency_code": "USD",
            "drafted_at": "2026-08-17T21:00:00Z",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf621",
            "tax_exclusive_amount": "100.00",
            "tax_amount": "10.00",
            "tax_inclusive_amount": "110.00",
            "credited_amount": "11.00",
            "amount_due": "99.00",
            "invoice_lines": [
                {
                    "line_number": 1,
                    "metric_code": "gen_ai_output_token",
                    "quantity": "100",
                    "unit_amount": "1.00",
                    "line_amount": "100.00",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_invoice_presentment(instance), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "200.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_invoice_presentment(unbalanced),
        )
        wrong_due = dict(instance)
        wrong_due["amount_due"] = "1.00"
        self.assertIn(
            "$: amount_due must equal inclusive minus credits and not go below zero",
            validate_invoice_presentment(wrong_due),
        )
        self.assertNotEqual(validate_invoice_presentment([]), ())
        non_string_tax = dict(instance)
        non_string_tax["tax_exclusive_amount"] = 100
        self.assertEqual(
            [error for error in validate_invoice_presentment(non_string_tax) if "exclusive plus tax" in error],
            [],
        )
        non_string_due = dict(instance)
        non_string_due["credited_amount"] = 11
        self.assertEqual(
            [error for error in validate_invoice_presentment(non_string_due) if "inclusive minus credits" in error],
            [],
        )
        scientific = dict(instance)
        scientific["tax_exclusive_amount"] = "1e2"
        scientific["tax_amount"] = "1e1"
        scientific["tax_inclusive_amount"] = "not-decimal"
        self.assertTrue(
            any("exact decimals" in error for error in validate_invoice_presentment(scientific))
        )
        due_scientific = dict(instance)
        due_scientific["credited_amount"] = "1e1"
        due_scientific["amount_due"] = "not-decimal"
        self.assertTrue(
            any("presentment amounts" in error for error in validate_invoice_presentment(due_scientific))
        )

    def test_collection_case_presentment_accepts_outstanding_and_rejects_posted(self) -> None:
        """A collection statement records exact outstanding and cannot claim posting."""
        schema = self._schema("collection-case-presentment.schema.json")
        instance = {
            "collection_case_presentment_contract_version": 1,
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "collection_outstanding": "100.00",
            "collection_case_status": "open",
            "opened_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "collect",
            "next_dunning_notice_code": "first_notice",
            "dunning_events": [],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_collection_case_presentment(instance), ())
        settled = dict(instance)
        settled["collection_outstanding"] = "0.00"
        settled["collection_case_status"] = "settled"
        settled["next_operator_action"] = "wait"
        settled.pop("next_dunning_notice_code")
        self.assertEqual(validate_collection_case_presentment(settled), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        negative = dict(instance)
        negative["collection_outstanding"] = "-1.00"
        self.assertIn(
            "$: collection_outstanding must not be negative",
            validate_collection_case_presentment(negative),
        )
        settled_due = dict(settled)
        settled_due["collection_outstanding"] = "1.00"
        self.assertIn(
            "$: settled cases must present zero outstanding",
            validate_collection_case_presentment(settled_due),
        )
        settled_collect = dict(settled)
        settled_collect["next_operator_action"] = "collect"
        self.assertIn(
            "$: settled cases must wait",
            validate_collection_case_presentment(settled_collect),
        )
        self.assertNotEqual(validate_collection_case_presentment([]), ())
        non_string = dict(instance)
        non_string["collection_outstanding"] = 100
        self.assertEqual(
            [
                error
                for error in validate_collection_case_presentment(non_string)
                if "collection_outstanding" in error and "exact decimal" in error
            ],
            [],
        )
        scientific = dict(instance)
        scientific["collection_outstanding"] = "not-decimal"
        self.assertTrue(
            any(
                "exact decimal" in error
                for error in validate_collection_case_presentment(scientific)
            )
        )

    def test_collection_aging_presentment_accepts_totals_and_rejects_posted(self) -> None:
        """Aging totals stay exact, unique by currency, and cannot claim posting."""
        schema = self._schema("collection-aging-presentment.schema.json")
        instance = {
            "collection_aging_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                {
                    "currency_code": "USD",
                    "current": {"case_count": 1, "outstanding_amount": "0.003705"},
                    "days_1_30": {"case_count": 0, "outstanding_amount": "0"},
                    "days_31_60": {"case_count": 0, "outstanding_amount": "0"},
                    "days_61_90": {"case_count": 0, "outstanding_amount": "0"},
                    "days_90_plus": {"case_count": 0, "outstanding_amount": "0"},
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_collection_aging_presentment(instance), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        negative = {
            "collection_aging_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                {
                    "currency_code": "USD",
                    "current": {"case_count": 1, "outstanding_amount": "-1.00"},
                    "days_1_30": {"case_count": 0, "outstanding_amount": "0"},
                    "days_31_60": {"case_count": 0, "outstanding_amount": "0"},
                    "days_61_90": {"case_count": 0, "outstanding_amount": "0"},
                    "days_90_plus": {"case_count": 0, "outstanding_amount": "0"},
                }
            ],
        }
        self.assertIn(
            "$.currencies[0].current: outstanding_amount must not be negative",
            validate_collection_aging_presentment(negative),
        )
        empty_nonzero = {
            "collection_aging_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                {
                    "currency_code": "USD",
                    "current": {"case_count": 0, "outstanding_amount": "1.00"},
                    "days_1_30": {"case_count": 0, "outstanding_amount": "0"},
                    "days_31_60": {"case_count": 0, "outstanding_amount": "0"},
                    "days_61_90": {"case_count": 0, "outstanding_amount": "0"},
                    "days_90_plus": {"case_count": 0, "outstanding_amount": "0"},
                }
            ],
        }
        self.assertIn(
            "$.currencies[0].current: empty buckets must be exact zero",
            validate_collection_aging_presentment(empty_nonzero),
        )
        duplicate = {
            "collection_aging_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                instance["currencies"][0],
                instance["currencies"][0],
            ],
        }
        self.assertIn(
            "$.currencies[1]: currency_code must be unique",
            validate_collection_aging_presentment(duplicate),
        )
        self.assertNotEqual(validate_collection_aging_presentment([]), ())
        self.assertNotEqual(validate_collection_aging_presentment({"currencies": "USD"}), ())
        mixed_rows = {
            "collection_aging_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                "USD",
                {
                    "currency_code": 1,
                    "current": "current",
                    "days_1_30": {"case_count": 0, "outstanding_amount": 1},
                    "days_31_60": {"case_count": 0, "outstanding_amount": "not-decimal"},
                    "days_61_90": {"case_count": 0, "outstanding_amount": "0"},
                    "days_90_plus": {"case_count": 0, "outstanding_amount": "0"},
                },
            ],
        }
        self.assertTrue(
            any(
                "exact decimal" in error
                for error in validate_collection_aging_presentment(mixed_rows)
            )
        )

    def test_account_statement_presentment_accepts_totals_and_rejects_posted(self) -> None:
        """Statement totals stay exact, unique by currency, and cannot claim posting."""
        schema = self._schema("account-statement-presentment.schema.json")
        instance = {
            "account_statement_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                {
                    "currency_code": "USD",
                    "issued_invoice_total": "0.003705",
                    "voided_invoice_total": "0",
                    "open_collection_remaining": "0.003705",
                    "applied_credit_total": "0",
                    "voided_credit_total": "0",
                    "write_off_total": "0",
                    "parked_unapplied_cash": "0",
                    "refunded_unapplied_cash": "0",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_account_statement_presentment(instance), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        duplicate = {
            "account_statement_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [instance["currencies"][0], instance["currencies"][0]],
        }
        self.assertIn(
            "$.currencies[1]: currency_code must be unique",
            validate_account_statement_presentment(duplicate),
        )
        negative = {
            "account_statement_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                {
                    "currency_code": "USD",
                    "issued_invoice_total": "-1.00",
                    "voided_invoice_total": "0",
                    "open_collection_remaining": "0",
                    "applied_credit_total": "0",
                    "voided_credit_total": "0",
                    "write_off_total": "0",
                    "parked_unapplied_cash": "0",
                    "refunded_unapplied_cash": 1,
                }
            ],
        }
        self.assertIn(
            "$.currencies[0].issued_invoice_total: amount must not be negative",
            validate_account_statement_presentment(negative),
        )
        self.assertNotEqual(validate_account_statement_presentment([]), ())
        self.assertNotEqual(validate_account_statement_presentment({"currencies": "USD"}), ())
        mixed_rows = {
            "account_statement_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "as_of": "2026-08-18T12:00:00Z",
            "currencies": [
                "USD",
                {
                    "currency_code": 1,
                    "issued_invoice_total": "not-decimal",
                    "voided_invoice_total": "0",
                    "open_collection_remaining": "0",
                    "applied_credit_total": "0",
                    "voided_credit_total": "0",
                    "write_off_total": "0",
                    "parked_unapplied_cash": "0",
                    "refunded_unapplied_cash": "0",
                },
            ],
        }
        self.assertTrue(
            any(
                "exact decimal" in error
                for error in validate_account_statement_presentment(mixed_rows)
            )
        )

    def test_rated_spend_presentment_accepts_product_totals_and_rejects_rerate(self) -> None:
        """Rated spend stays exact, unique by currency and product, and cannot claim a re-rate."""
        schema = self._schema("rated-spend-presentment.schema.json")
        instance = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "rated_amount": "0.003705",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_rated_spend_presentment(instance), ())
        posted = dict(instance)
        posted["group_by"] = "project"
        self.assertIn(
            "$: additional property is not allowed: group_by",
            validate_schema_instance(schema, posted),
        )
        duplicate = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [instance["products"][0], instance["products"][0]],
        }
        self.assertIn(
            "$.products[1]: currency_code and product_code must be unique",
            validate_rated_spend_presentment(duplicate),
        )
        negative = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "rated_amount": "-1.00",
                }
            ],
        }
        self.assertIn(
            "$.products[0].rated_amount: amount must not be negative",
            validate_rated_spend_presentment(negative),
        )
        self.assertNotEqual(validate_rated_spend_presentment([]), ())
        self.assertNotEqual(validate_rated_spend_presentment({"products": "USD"}), ())
        mixed_rows = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [
                "USD",
                {
                    "currency_code": 1,
                    "product_code": "contextual_orchestrator",
                    "rated_amount": 1,
                },
                {
                    "currency_code": "USD",
                    "product_code": "contextual_memory",
                    "rated_amount": "not-decimal",
                },
            ],
        }
        self.assertTrue(
            any(
                "exact decimal" in error
                for error in validate_rated_spend_presentment(mixed_rows)
            )
        )
        project_row = {
            "currency_code": "USD",
            "product_code": "contextual_orchestrator",
            "project_reference": "urn:cwl:tenant_001:project:metering",
            "rated_amount": "0.003705",
        }
        project_instance = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [project_row],
        }
        self.assertEqual(validate_schema_instance(schema, project_instance), ())
        self.assertEqual(validate_rated_spend_presentment(project_instance), ())
        split_projects = {
            **project_instance,
            "products": [
                project_row,
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "project_reference": "urn:cwl:tenant_001:project:memory",
                    "rated_amount": "0.000085",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, split_projects), ())
        self.assertEqual(validate_rated_spend_presentment(split_projects), ())
        duplicate_project = {**project_instance, "products": [project_row, project_row]}
        self.assertIn(
            "$.products[1]: currency_code, product_code, and project_reference must be unique",
            validate_rated_spend_presentment(duplicate_project),
        )
        credential_row = {
            "currency_code": "USD",
            "product_code": "contextual_orchestrator",
            "credential_reference": "urn:cwl:tenant_001:credential_record:019d7003",
            "rated_amount": "0.003705",
        }
        credential_instance = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [credential_row],
        }
        self.assertEqual(validate_schema_instance(schema, credential_instance), ())
        self.assertEqual(validate_rated_spend_presentment(credential_instance), ())
        split_credentials = {
            **credential_instance,
            "products": [
                credential_row,
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "credential_reference": "urn:cwl:tenant_001:credential_record:019d7004",
                    "rated_amount": "0.000085",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, split_credentials), ())
        self.assertEqual(validate_rated_spend_presentment(split_credentials), ())
        duplicate_credential = {
            **credential_instance,
            "products": [credential_row, credential_row],
        }
        self.assertIn(
            "$.products[1]: currency_code, product_code, and credential_reference must be unique",
            validate_rated_spend_presentment(duplicate_credential),
        )
        principal_row = {
            "currency_code": "USD",
            "product_code": "contextual_orchestrator",
            "billing_principal_reference": "urn:cwl:tenant_001:billing_principal:019d7002",
            "rated_amount": "0.003705",
        }
        principal_instance = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [principal_row],
        }
        self.assertEqual(validate_schema_instance(schema, principal_instance), ())
        self.assertEqual(validate_rated_spend_presentment(principal_instance), ())
        split_principals = {
            **principal_instance,
            "products": [
                principal_row,
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "billing_principal_reference": "urn:cwl:tenant_001:billing_principal:019d7005",
                    "rated_amount": "0.000085",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, split_principals), ())
        self.assertEqual(validate_rated_spend_presentment(split_principals), ())
        duplicate_principal = {
            **principal_instance,
            "products": [principal_row, principal_row],
        }
        self.assertIn(
            "$.products[1]: currency_code, product_code, and "
            "billing_principal_reference must be unique",
            validate_rated_spend_presentment(duplicate_principal),
        )
        cost_center_row = {
            "currency_code": "USD",
            "product_code": "contextual_orchestrator",
            "cost_center_reference": "urn:cwl:tenant_001:cost_center:platform",
            "rated_amount": "0.003705",
        }
        cost_center_instance = {
            "rated_spend_presentment_contract_version": 1,
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7001-0000-7000-8000-000000000001",
            "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "products": [cost_center_row],
        }
        self.assertEqual(validate_schema_instance(schema, cost_center_instance), ())
        self.assertEqual(validate_rated_spend_presentment(cost_center_instance), ())
        split_cost_centers = {
            **cost_center_instance,
            "products": [
                cost_center_row,
                {
                    "currency_code": "USD",
                    "product_code": "contextual_orchestrator",
                    "cost_center_reference": "urn:cwl:tenant_001:cost_center:memory",
                    "rated_amount": "0.000085",
                },
            ],
        }
        self.assertEqual(validate_schema_instance(schema, split_cost_centers), ())
        self.assertEqual(validate_rated_spend_presentment(split_cost_centers), ())
        duplicate_cost_center = {
            **cost_center_instance,
            "products": [cost_center_row, cost_center_row],
        }
        self.assertIn(
            "$.products[1]: currency_code, product_code, and "
            "cost_center_reference must be unique",
            validate_rated_spend_presentment(duplicate_cost_center),
        )

    def test_payment_intent_presentment_accepts_amount_and_rejects_captured(self) -> None:
        """A payment-intent statement records exact amount and cannot claim capture."""
        schema = self._schema("payment-intent-presentment.schema.json")
        instance = {
            "payment_intent_presentment_contract_version": 1,
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "payment_amount": "0.003705",
            "payment_intent_status": "projected",
            "projected_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "record_receipt",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_payment_intent_presentment(instance), ())
        cancelled = dict(instance)
        cancelled["payment_intent_status"] = "cancelled"
        cancelled["next_operator_action"] = "wait"
        self.assertEqual(validate_payment_intent_presentment(cancelled), ())
        captured = dict(instance)
        captured["payment_intent_status"] = "captured"
        self.assertIn(
            "$.payment_intent_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        negative = dict(instance)
        negative["payment_amount"] = "-1.00"
        self.assertIn(
            "$: payment_amount must not be negative",
            validate_payment_intent_presentment(negative),
        )
        projected_wait = dict(instance)
        projected_wait["next_operator_action"] = "wait"
        self.assertIn(
            "$: projected intents must record a receipt",
            validate_payment_intent_presentment(projected_wait),
        )
        cancelled_collect = dict(cancelled)
        cancelled_collect["next_operator_action"] = "record_receipt"
        self.assertIn(
            "$: cancelled or rejected intents must wait",
            validate_payment_intent_presentment(cancelled_collect),
        )
        rejected_collect = dict(instance)
        rejected_collect["payment_intent_status"] = "rejected"
        rejected_collect["next_operator_action"] = "record_receipt"
        self.assertIn(
            "$: cancelled or rejected intents must wait",
            validate_payment_intent_presentment(rejected_collect),
        )
        self.assertNotEqual(validate_payment_intent_presentment([]), ())
        non_string = dict(instance)
        non_string["payment_amount"] = 100
        self.assertEqual(
            [
                error
                for error in validate_payment_intent_presentment(non_string)
                if "payment_amount" in error and "exact decimal" in error
            ],
            [],
        )
        scientific = dict(instance)
        scientific["payment_amount"] = "not-decimal"
        self.assertTrue(
            any("exact decimal" in error for error in validate_payment_intent_presentment(scientific))
        )

    def test_payment_receipt_presentment_accepts_amount_and_rejects_posted(self) -> None:
        """A payment-receipt statement records exact amounts and cannot claim posting."""
        schema = self._schema("payment-receipt-presentment.schema.json")
        instance = {
            "payment_receipt_presentment_contract_version": 1,
            "payment_receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf650",
            "tenant_reference": "urn:cwl:tenant_001",
            "payment_intent_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf640",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "currency_code": "USD",
            "received_amount": "0.003705",
            "remaining_outstanding_amount": "0.00",
            "payment_receipt_status": "applied",
            "collection_case_status": "settled",
            "received_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "drain_or_wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_payment_receipt_presentment(instance), ())
        partial = dict(instance)
        partial["received_amount"] = "0.001"
        partial["remaining_outstanding_amount"] = "0.002705"
        partial["collection_case_status"] = "open"
        partial["next_operator_action"] = "record_receipt"
        self.assertEqual(validate_payment_receipt_presentment(partial), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        captured = dict(instance)
        captured["payment_receipt_status"] = "captured"
        self.assertIn(
            "$.payment_receipt_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )
        negative_received = dict(instance)
        negative_received["received_amount"] = "-1.00"
        self.assertIn(
            "$: received_amount must not be negative",
            validate_payment_receipt_presentment(negative_received),
        )
        negative_remaining = dict(instance)
        negative_remaining["remaining_outstanding_amount"] = "-1.00"
        self.assertIn(
            "$: remaining_outstanding_amount must not be negative",
            validate_payment_receipt_presentment(negative_remaining),
        )
        settled_collect = dict(instance)
        settled_collect["next_operator_action"] = "record_receipt"
        self.assertIn(
            "$: settled receipts must drain or wait",
            validate_payment_receipt_presentment(settled_collect),
        )
        residual_wait = dict(partial)
        residual_wait["next_operator_action"] = "drain_or_wait"
        self.assertIn(
            "$: residual receipts must record another receipt",
            validate_payment_receipt_presentment(residual_wait),
        )
        self.assertNotEqual(validate_payment_receipt_presentment([]), ())
        non_string = dict(instance)
        non_string["received_amount"] = 100
        non_string["remaining_outstanding_amount"] = 0
        self.assertEqual(
            [
                error
                for error in validate_payment_receipt_presentment(non_string)
                if "exact decimal" in error
            ],
            [],
        )
        bad_received = dict(instance)
        bad_received["received_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "received_amount must be an exact decimal" in error
                for error in validate_payment_receipt_presentment(bad_received)
            )
        )
        bad_remaining = dict(instance)
        bad_remaining["remaining_outstanding_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "remaining_outstanding_amount must be an exact decimal" in error
                for error in validate_payment_receipt_presentment(bad_remaining)
            )
        )

    def test_credit_adjustment_presentment_accepts_split_and_rejects_posted(self) -> None:
        """A credit statement records exact amounts and cannot claim posting."""
        schema = self._schema("credit-adjustment-presentment.schema.json")
        instance = {
            "credit_adjustment_presentment_contract_version": 1,
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "credit_amount": "0.003705",
            "tax_exclusive_amount": "0.003705",
            "tax_amount": "0",
            "credit_adjustment_status": "recorded",
            "recorded_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_credit_adjustment_presentment(instance), ())
        taxed = dict(instance)
        taxed["credit_amount"] = "11.00"
        taxed["tax_exclusive_amount"] = "10.00"
        taxed["tax_amount"] = "1.00"
        self.assertEqual(validate_credit_adjustment_presentment(taxed), ())
        posted = dict(instance)
        posted["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        captured = dict(instance)
        captured["credit_adjustment_status"] = "posted"
        self.assertIn(
            "$.credit_adjustment_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )
        negative = dict(instance)
        negative["credit_amount"] = "-1.00"
        self.assertIn(
            "$: credit_amount must not be negative",
            validate_credit_adjustment_presentment(negative),
        )
        unbalanced = dict(taxed)
        unbalanced["tax_amount"] = "2.00"
        self.assertIn(
            "$: tax_exclusive_amount plus tax_amount must equal credit_amount",
            validate_credit_adjustment_presentment(unbalanced),
        )
        recorded_collect = dict(instance)
        recorded_collect["next_operator_action"] = "credit"
        self.assertIn(
            "$: recorded credits must wait",
            validate_credit_adjustment_presentment(recorded_collect),
        )
        self.assertNotEqual(validate_credit_adjustment_presentment([]), ())
        non_string = dict(instance)
        non_string["credit_amount"] = 11
        self.assertEqual(
            [
                error
                for error in validate_credit_adjustment_presentment(non_string)
                if "exact decimal" in error
            ],
            [],
        )
        bad_credit = dict(instance)
        bad_credit["credit_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "credit_amount must be an exact decimal" in error
                for error in validate_credit_adjustment_presentment(bad_credit)
            )
        )
        bad_exclusive = dict(instance)
        bad_exclusive["tax_exclusive_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_exclusive_amount must be an exact decimal" in error
                for error in validate_credit_adjustment_presentment(bad_exclusive)
            )
        )
        bad_tax = dict(instance)
        bad_tax["tax_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_amount must be an exact decimal" in error
                for error in validate_credit_adjustment_presentment(bad_tax)
            )
        )
        negative_exclusive = dict(instance)
        negative_exclusive["tax_exclusive_amount"] = "-1.00"
        self.assertIn(
            "$: tax_exclusive_amount must not be negative",
            validate_credit_adjustment_presentment(negative_exclusive),
        )
        negative_tax = dict(instance)
        negative_tax["tax_amount"] = "-1.00"
        self.assertIn(
            "$: tax_amount must not be negative",
            validate_credit_adjustment_presentment(negative_tax),
        )

    def test_spend_budget_accepts_published_amount_and_closed_codes(self) -> None:
        """A spend-budget contract records an exact amount and published status."""
        schema = self._schema("spend-budget.schema.json")
        instance = {
            "spend_budget_contract_version": 1,
            "spend_budget_outcome_code": "accepted",
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "100.00",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "published_at": "2026-08-18T15:00:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget(instance), ())
        floated = dict(instance, budget_amount=100.00)
        self.assertTrue(validate_schema_instance(schema, floated))
        extra = dict(instance, group_by="product")
        self.assertIn(
            "$: additional property is not allowed: group_by",
            validate_schema_instance(schema, extra),
        )
        items = dict(instance, items=[])
        self.assertIn(
            "$: additional property is not allowed: items",
            validate_schema_instance(schema, items),
        )
        cursor = dict(instance, cursor="x")
        self.assertIn(
            "$: additional property is not allowed: cursor",
            validate_schema_instance(schema, cursor),
        )
        posted = dict(instance, proposal_status="posted")
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        earnings = dict(instance, retained_earnings="1")
        self.assertIn(
            "$: additional property is not allowed: retained_earnings",
            validate_schema_instance(schema, earnings),
        )
        zeroed = dict(instance, budget_amount="0")
        self.assertIn("$: budget_amount must be greater than zero", validate_spend_budget(zeroed))
        bad_amount = dict(instance, budget_amount="not-decimal")
        self.assertTrue(
            any(
                "budget_amount must be an exact decimal" in error
                for error in validate_spend_budget(bad_amount)
            )
        )
        waiting = dict(instance, next_operator_action="collect")
        self.assertIn("$: published spend budgets must wait", validate_spend_budget(waiting))
        rejected = {
            "spend_budget_contract_version": 1,
            "spend_budget_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected spend budgets must include rejection_reason_code",
            validate_spend_budget(rejected),
        )
        self.assertNotEqual(validate_spend_budget([]), ())
        missing_id = dict(instance)
        del missing_id["spend_budget_id"]
        self.assertIn(
            "$: accepted spend budgets must include spend_budget_id",
            validate_spend_budget(missing_id),
        )
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn("$: spend budget must not include card_pan", validate_spend_budget(pan))
        earnings_semantic = dict(instance, retained_earnings="1")
        self.assertIn(
            "$: spend budget must not include retained_earnings",
            validate_spend_budget(earnings_semantic),
        )
        unknown_reason = {
            "spend_budget_contract_version": 1,
            "spend_budget_outcome_code": "rejected",
            "rejection_reason_code": "tax_exempt",
        }
        self.assertIn(
            "$.rejection_reason_code: value is not in the allowed enumeration",
            validate_schema_instance(schema, unknown_reason),
        )
        replayed = dict(instance, spend_budget_outcome_code="duplicate_replay")
        self.assertEqual(validate_spend_budget(replayed), ())
        rejected_known = {
            "spend_budget_contract_version": 1,
            "spend_budget_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_spend_budget(rejected_known), ())
        non_string_amount = dict(instance, budget_amount=100)
        self.assertNotEqual(validate_spend_budget(non_string_amount), ())
        unpublished = dict(instance)
        unpublished.pop("spend_budget_status")
        self.assertIn(
            "$: accepted spend budgets must include spend_budget_status",
            validate_spend_budget(unpublished),
        )
        posted_outcome = {
            "spend_budget_contract_version": 1,
            "spend_budget_outcome_code": "posted",
        }
        self.assertNotEqual(validate_spend_budget(posted_outcome), ())

    def test_spend_budget_over_signal_accepts_over_and_rejects_remaining(self) -> None:
        """An over-signal contract records exact over and omits remaining."""
        schema = self._schema("spend-budget-over-signal.schema.json")
        instance = {
            "spend_budget_over_signal_contract_version": 1,
            "spend_budget_over_signal_outcome_code": "accepted",
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "0.001",
            "over_amount": "12.345",
            "utilization_status": "over",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "spend_budget_contract_version": 1,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_over_signal(instance), ())
        floated = dict(instance, over_amount=12.345)
        self.assertTrue(validate_schema_instance(schema, floated))
        remaining = dict(instance, remaining_amount="0")
        self.assertIn(
            "$: additional property is not allowed: remaining_amount",
            validate_schema_instance(schema, remaining),
        )
        self.assertIn(
            "$: spend budget over signal must not include remaining_amount",
            validate_spend_budget_over_signal(remaining),
        )
        zeroed = dict(instance, budget_amount="0")
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_over_signal(zeroed),
        )
        bad_amount = dict(instance, budget_amount="not-decimal")
        self.assertTrue(
            any(
                "budget_amount must be an exact decimal" in error
                for error in validate_spend_budget_over_signal(bad_amount)
            )
        )
        negative_over = dict(instance, over_amount="-1")
        self.assertIn(
            "$: over_amount must be a non-negative exact decimal",
            validate_spend_budget_over_signal(negative_over),
        )
        zero_over = dict(instance, over_amount="0")
        self.assertIn(
            "$: over observations must include a positive over_amount",
            validate_spend_budget_over_signal(zero_over),
        )
        under = dict(instance, utilization_status="under", over_amount="0")
        self.assertEqual(validate_spend_budget_over_signal(under), ())
        under_with_over = dict(instance, utilization_status="under", over_amount="1")
        self.assertIn(
            "$: under and at observations must have zero over_amount",
            validate_spend_budget_over_signal(under_with_over),
        )
        at_row = dict(instance, utilization_status="at", over_amount="0")
        self.assertEqual(validate_spend_budget_over_signal(at_row), ())
        waiting = dict(instance, next_operator_action="collect")
        self.assertIn("$: published spend budgets must wait", validate_spend_budget_over_signal(waiting))
        rejected = {
            "spend_budget_over_signal_contract_version": 1,
            "spend_budget_over_signal_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected over signals must include rejection_reason_code",
            validate_spend_budget_over_signal(rejected),
        )
        self.assertNotEqual(validate_spend_budget_over_signal([]), ())
        missing_id = dict(instance)
        del missing_id["spend_budget_id"]
        self.assertIn(
            "$: accepted over signals must include spend_budget_id",
            validate_spend_budget_over_signal(missing_id),
        )
        missing_budget = dict(instance)
        del missing_budget["budget_amount"]
        self.assertIn(
            "$: accepted over signals must include budget_amount",
            validate_spend_budget_over_signal(missing_budget),
        )
        unpublished = dict(instance, spend_budget_status="draft")
        unpublished_errors = validate_spend_budget_over_signal(unpublished)
        self.assertTrue(unpublished_errors)
        self.assertNotIn("$: published spend budgets must wait", unpublished_errors)
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: spend budget over signal must not include card_pan",
            validate_spend_budget_over_signal(pan),
        )
        earnings = dict(instance, retained_earnings="1")
        self.assertIn(
            "$: spend budget over signal must not include retained_earnings",
            validate_spend_budget_over_signal(earnings),
        )
        replayed = dict(instance, spend_budget_over_signal_outcome_code="duplicate_replay")
        self.assertEqual(validate_spend_budget_over_signal(replayed), ())
        rejected_known = {
            "spend_budget_over_signal_contract_version": 1,
            "spend_budget_over_signal_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_spend_budget_over_signal(rejected_known), ())
        bad_over = dict(instance, over_amount="not-decimal")
        self.assertTrue(
            any(
                "over_amount must be an exact decimal" in error
                for error in validate_spend_budget_over_signal(bad_over)
            )
        )
        posted_outcome = {
            "spend_budget_over_signal_contract_version": 1,
            "spend_budget_over_signal_outcome_code": "posted",
        }
        self.assertNotEqual(validate_spend_budget_over_signal(posted_outcome), ())

    def test_spend_budget_approaching_signal_accepts_at_and_rejects_over(self) -> None:
        """An approaching-signal contract records exact remaining and omits over."""
        schema = self._schema("spend-budget-approaching-signal.schema.json")
        instance = {
            "spend_budget_approaching_signal_contract_version": 1,
            "spend_budget_approaching_signal_outcome_code": "accepted",
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "12.345",
            "remaining_amount": "0",
            "utilization_status": "at",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "spend_budget_contract_version": 1,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_approaching_signal(instance), ())
        floated = dict(instance, remaining_amount=0.0)
        self.assertTrue(validate_schema_instance(schema, floated))
        over = dict(instance, over_amount="0")
        self.assertIn(
            "$: additional property is not allowed: over_amount",
            validate_schema_instance(schema, over),
        )
        self.assertIn(
            "$: spend budget approaching signal must not include over_amount",
            validate_spend_budget_approaching_signal(over),
        )
        zeroed = dict(instance, budget_amount="0")
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_approaching_signal(zeroed),
        )
        bad_amount = dict(instance, budget_amount="not-decimal")
        self.assertTrue(
            any(
                "budget_amount must be an exact decimal" in error
                for error in validate_spend_budget_approaching_signal(bad_amount)
            )
        )
        negative_remaining = dict(instance, remaining_amount="-1")
        self.assertIn(
            "$: remaining_amount must be a non-negative exact decimal",
            validate_spend_budget_approaching_signal(negative_remaining),
        )
        at_with_remaining = dict(instance, remaining_amount="1")
        self.assertIn(
            "$: at observations must have zero remaining_amount",
            validate_spend_budget_approaching_signal(at_with_remaining),
        )
        under = dict(instance, utilization_status="under", remaining_amount="1")
        self.assertEqual(validate_spend_budget_approaching_signal(under), ())
        under_zero = dict(instance, utilization_status="under", remaining_amount="0")
        self.assertIn(
            "$: under observations must include a positive remaining_amount",
            validate_spend_budget_approaching_signal(under_zero),
        )
        over_row = dict(instance, utilization_status="over", remaining_amount="0")
        self.assertEqual(validate_spend_budget_approaching_signal(over_row), ())
        over_with_remaining = dict(instance, utilization_status="over", remaining_amount="1")
        self.assertIn(
            "$: over observations must have zero remaining_amount",
            validate_spend_budget_approaching_signal(over_with_remaining),
        )
        waiting = dict(instance, next_operator_action="collect")
        self.assertIn(
            "$: published spend budgets must wait",
            validate_spend_budget_approaching_signal(waiting),
        )
        rejected = {
            "spend_budget_approaching_signal_contract_version": 1,
            "spend_budget_approaching_signal_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected approaching signals must include rejection_reason_code",
            validate_spend_budget_approaching_signal(rejected),
        )
        self.assertNotEqual(validate_spend_budget_approaching_signal([]), ())
        missing_id = dict(instance)
        del missing_id["spend_budget_id"]
        self.assertIn(
            "$: accepted approaching signals must include spend_budget_id",
            validate_spend_budget_approaching_signal(missing_id),
        )
        missing_budget = dict(instance)
        del missing_budget["budget_amount"]
        self.assertIn(
            "$: accepted approaching signals must include budget_amount",
            validate_spend_budget_approaching_signal(missing_budget),
        )
        unpublished = dict(instance, spend_budget_status="draft")
        unpublished_errors = validate_spend_budget_approaching_signal(unpublished)
        self.assertTrue(unpublished_errors)
        self.assertNotIn("$: published spend budgets must wait", unpublished_errors)
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: spend budget approaching signal must not include card_pan",
            validate_spend_budget_approaching_signal(pan),
        )
        earnings = dict(instance, retained_earnings="1")
        self.assertIn(
            "$: spend budget approaching signal must not include retained_earnings",
            validate_spend_budget_approaching_signal(earnings),
        )
        replayed = dict(instance, spend_budget_approaching_signal_outcome_code="duplicate_replay")
        self.assertEqual(validate_spend_budget_approaching_signal(replayed), ())
        rejected_known = {
            "spend_budget_approaching_signal_contract_version": 1,
            "spend_budget_approaching_signal_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_spend_budget_approaching_signal(rejected_known), ())
        bad_remaining = dict(instance, remaining_amount="not-decimal")
        self.assertTrue(
            any(
                "remaining_amount must be an exact decimal" in error
                for error in validate_spend_budget_approaching_signal(bad_remaining)
            )
        )
        posted_outcome = {
            "spend_budget_approaching_signal_contract_version": 1,
            "spend_budget_approaching_signal_outcome_code": "posted",
        }
        self.assertNotEqual(validate_spend_budget_approaching_signal(posted_outcome), ())

    def test_spend_budget_approaching_signal_presentment_reuses_existing_envelopes(self) -> None:
        """GET presentment nests the existing approaching-signal and outbox envelopes."""
        schema = self._schema("spend-budget-approaching-signal-presentment.schema.json")
        approaching_signal = {
            "spend_budget_approaching_signal_contract_version": 1,
            "spend_budget_approaching_signal_outcome_code": "accepted",
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "100.00",
            "remaining_amount": "0",
            "utilization_status": "at",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "spend_budget_contract_version": 1,
            "next_operator_action": "wait",
        }
        outbox = {
            "webhook_outbox_event_presentment_contract_version": 1,
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf682",
            "tenant_reference": "urn:cwl:tenant_001",
            "event_type_code": "spend_budget.approaching",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "payload_hash": "sha256:" + "b" * 64,
            "occurred_at": "2026-08-18T15:00:00Z",
            "enqueued_at": "2026-08-18T15:00:00Z",
            "delivery_status": "pending",
            "attempted_delivery_count": 0,
            "next_operator_action": "run_deliveries",
        }
        instance = {
            "spend_budget_approaching_signal_presentment_contract_version": 1,
            "approaching_signal": approaching_signal,
            "webhook_outbox_events": [outbox],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(instance), ())
        under = {
            "spend_budget_approaching_signal_presentment_contract_version": 1,
            "approaching_signal": dict(
                approaching_signal, utilization_status="under", remaining_amount="99.996295"
            ),
            "webhook_outbox_events": [],
        }
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(under), ())
        self.assertNotEqual(validate_spend_budget_approaching_signal_presentment([]), ())
        over_amount = dict(instance, over_amount="0")
        self.assertIn(
            "$: approaching-signal presentment must not include over_amount",
            validate_spend_budget_approaching_signal_presentment(over_amount),
        )
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: approaching-signal presentment must not include card_pan",
            validate_spend_budget_approaching_signal_presentment(pan),
        )
        earnings = dict(instance, retained_earnings="0")
        self.assertIn(
            "$: approaching-signal presentment must not include retained_earnings",
            validate_spend_budget_approaching_signal_presentment(earnings),
        )
        leaked = dict(instance, payload_json="{}")
        self.assertIn(
            "$: approaching-signal presentment must not include payload_json",
            validate_spend_budget_approaching_signal_presentment(leaked),
        )
        zeroed = dict(instance, approaching_signal=dict(approaching_signal, budget_amount="0"))
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_approaching_signal_presentment(zeroed),
        )
        two_rows = dict(instance, webhook_outbox_events=[outbox, outbox])
        self.assertIn(
            "$: approaching-signal presentment has at most one spend_budget.approaching row",
            validate_spend_budget_approaching_signal_presentment(two_rows),
        )
        hollow_row = dict(instance, webhook_outbox_events=["pending"])
        self.assertNotEqual(validate_spend_budget_approaching_signal_presentment(hollow_row), ())
        published = dict(outbox, event_type_code="spend_budget.published")
        wrong_type = dict(instance, webhook_outbox_events=[published])
        self.assertTrue(
            any(
                "event_type_code must be spend_budget.approaching" in error
                for error in validate_spend_budget_approaching_signal_presentment(wrong_type)
            )
        )
        mismatched = dict(outbox, source_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf699")
        crossed = dict(instance, webhook_outbox_events=[mismatched])
        self.assertTrue(
            any(
                "source_id must match approaching_signal.spend_budget_id" in error
                for error in validate_spend_budget_approaching_signal_presentment(crossed)
            )
        )
        self.assertNotEqual(
            validate_spend_budget_approaching_signal_presentment(
                dict(instance, approaching_signal="x")
            ),
            (),
        )
        rejected_presentment = {
            "spend_budget_approaching_signal_presentment_contract_version": 1,
            "approaching_signal": {
                "spend_budget_approaching_signal_contract_version": 1,
                "spend_budget_approaching_signal_outcome_code": "rejected",
                "rejection_reason_code": "tenant_not_found",
            },
            "webhook_outbox_events": [outbox],
        }
        self.assertEqual(validate_spend_budget_approaching_signal_presentment(rejected_presentment), ())
        missing_type = dict(outbox)
        missing_type.pop("event_type_code")
        self.assertNotEqual(
            validate_spend_budget_approaching_signal_presentment(
                dict(instance, webhook_outbox_events=[missing_type])
            ),
            (),
        )
        self.assertNotEqual(
            validate_spend_budget_approaching_signal_presentment(
                dict(instance, webhook_outbox_events=None)
            ),
            (),
        )

    def test_spend_budget_over_signal_presentment_reuses_existing_envelopes(self) -> None:
        """GET presentment nests the existing over-signal and outbox envelopes."""
        schema = self._schema("spend-budget-over-signal-presentment.schema.json")
        over_signal = {
            "spend_budget_over_signal_contract_version": 1,
            "spend_budget_over_signal_outcome_code": "accepted",
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "0.001",
            "over_amount": "12.345",
            "utilization_status": "over",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "spend_budget_contract_version": 1,
            "next_operator_action": "wait",
        }
        outbox = {
            "webhook_outbox_event_presentment_contract_version": 1,
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf682",
            "tenant_reference": "urn:cwl:tenant_001",
            "event_type_code": "spend_budget.over",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "payload_hash": "sha256:" + "b" * 64,
            "occurred_at": "2026-08-18T15:00:00Z",
            "enqueued_at": "2026-08-18T15:00:00Z",
            "delivery_status": "pending",
            "attempted_delivery_count": 0,
            "next_operator_action": "run_deliveries",
        }
        instance = {
            "spend_budget_over_signal_presentment_contract_version": 1,
            "over_signal": over_signal,
            "webhook_outbox_events": [outbox],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_over_signal_presentment(instance), ())
        under = {
            "spend_budget_over_signal_presentment_contract_version": 1,
            "over_signal": dict(over_signal, utilization_status="under", over_amount="0"),
            "webhook_outbox_events": [],
        }
        self.assertEqual(validate_spend_budget_over_signal_presentment(under), ())
        self.assertNotEqual(validate_spend_budget_over_signal_presentment([]), ())
        remaining = dict(instance, remaining_amount="0")
        self.assertIn(
            "$: over-signal presentment must not include remaining_amount",
            validate_spend_budget_over_signal_presentment(remaining),
        )
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: over-signal presentment must not include card_pan",
            validate_spend_budget_over_signal_presentment(pan),
        )
        earnings = dict(instance, retained_earnings="0")
        self.assertIn(
            "$: over-signal presentment must not include retained_earnings",
            validate_spend_budget_over_signal_presentment(earnings),
        )
        leaked = dict(instance, payload_json="{}")
        self.assertIn(
            "$: over-signal presentment must not include payload_json",
            validate_spend_budget_over_signal_presentment(leaked),
        )
        zeroed = dict(instance, over_signal=dict(over_signal, budget_amount="0"))
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_over_signal_presentment(zeroed),
        )
        two_rows = dict(instance, webhook_outbox_events=[outbox, outbox])
        self.assertIn(
            "$: over-signal presentment has at most one spend_budget.over row",
            validate_spend_budget_over_signal_presentment(two_rows),
        )
        hollow_row = dict(instance, webhook_outbox_events=["pending"])
        self.assertNotEqual(validate_spend_budget_over_signal_presentment(hollow_row), ())
        published = dict(outbox, event_type_code="spend_budget.published")
        wrong_type = dict(instance, webhook_outbox_events=[published])
        self.assertTrue(
            any(
                "event_type_code must be spend_budget.over" in error
                for error in validate_spend_budget_over_signal_presentment(wrong_type)
            )
        )
        mismatched = dict(outbox, source_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf699")
        crossed = dict(instance, webhook_outbox_events=[mismatched])
        self.assertTrue(
            any(
                "source_id must match over_signal.spend_budget_id" in error
                for error in validate_spend_budget_over_signal_presentment(crossed)
            )
        )
        self.assertNotEqual(
            validate_spend_budget_over_signal_presentment(dict(instance, over_signal="x")),
            (),
        )
        rejected_presentment = {
            "spend_budget_over_signal_presentment_contract_version": 1,
            "over_signal": {
                "spend_budget_over_signal_contract_version": 1,
                "spend_budget_over_signal_outcome_code": "rejected",
                "rejection_reason_code": "tenant_not_found",
            },
            "webhook_outbox_events": [outbox],
        }
        self.assertEqual(validate_spend_budget_over_signal_presentment(rejected_presentment), ())
        missing_type = dict(outbox)
        missing_type.pop("event_type_code")
        self.assertNotEqual(
            validate_spend_budget_over_signal_presentment(
                dict(instance, webhook_outbox_events=[missing_type])
            ),
            (),
        )
        self.assertNotEqual(
            validate_spend_budget_over_signal_presentment(
                dict(instance, webhook_outbox_events=None)
            ),
            (),
        )

    def test_spend_budget_presentment_accepts_published_row_and_rejects_posted(self) -> None:
        """A spend-budget statement records exact amounts and cannot claim posting."""
        schema = self._schema("spend-budget-presentment.schema.json")
        instance = {
            "spend_budget_presentment_contract_version": 1,
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "100.00",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "source_payload_hash": "sha256:" + "6" * 64,
            "published_at": "2026-08-18T15:00:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_presentment(instance), ())
        posted = dict(instance, proposal_status="posted")
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        captured = dict(instance, spend_budget_status="posted")
        self.assertIn(
            "$.spend_budget_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, captured),
        )
        zeroed = dict(instance, budget_amount="0")
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_presentment(zeroed),
        )
        waiting = dict(instance, next_operator_action="collect")
        self.assertIn(
            "$.next_operator_action: value is not in the allowed enumeration",
            validate_schema_instance(schema, waiting),
        )
        collect = dict(instance)
        collect["next_operator_action"] = "wait"
        collect["spend_budget_status"] = "published"
        self.assertEqual(validate_spend_budget_presentment(collect), ())
        self.assertNotEqual(validate_spend_budget_presentment([]), ())
        bad_amount = dict(instance, budget_amount="not-decimal")
        self.assertTrue(
            any(
                "budget_amount must be an exact decimal" in error
                for error in validate_spend_budget_presentment(bad_amount)
            )
        )
        extra = dict(instance, items=[])
        self.assertIn(
            "$: additional property is not allowed: items",
            validate_schema_instance(schema, extra),
        )
        waiting_presentment = dict(instance, next_operator_action="collect")
        self.assertIn(
            "$: published spend budgets must wait",
            validate_spend_budget_presentment(waiting_presentment),
        )
        pan_presentment = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: spend budget presentment must not include card_pan",
            validate_spend_budget_presentment(pan_presentment),
        )
        non_string_presentment = dict(instance, budget_amount=100)
        self.assertNotEqual(validate_spend_budget_presentment(non_string_presentment), ())

    def test_spend_budget_evaluation_accepts_under_at_over_and_rejects_posted(self) -> None:
        """An evaluation statement records exact remaining/over and cannot claim posting."""
        schema = self._schema("spend-budget-evaluation-presentment.schema.json")
        instance = {
            "spend_budget_evaluation_presentment_contract_version": 1,
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "billing_account_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf681",
            "currency_code": "USD",
            "budget_amount": "100.00",
            "rated_amount": "40.00",
            "remaining_amount": "60.00",
            "over_amount": "0",
            "utilization_status": "under",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_spend_budget_evaluation_presentment(instance), ())
        at_budget = dict(instance, rated_amount="100.00", remaining_amount="0", over_amount="0")
        at_budget["utilization_status"] = "at"
        self.assertEqual(validate_spend_budget_evaluation_presentment(at_budget), ())
        over_budget = dict(instance, rated_amount="125.00", remaining_amount="0", over_amount="25.00")
        over_budget["utilization_status"] = "over"
        self.assertEqual(validate_spend_budget_evaluation_presentment(over_budget), ())
        posted = dict(instance, proposal_status="posted")
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, posted),
        )
        zeroed = dict(instance, budget_amount="0")
        self.assertIn(
            "$: budget_amount must be greater than zero",
            validate_spend_budget_evaluation_presentment(zeroed),
        )
        negative = dict(instance, remaining_amount="-1")
        self.assertTrue(
            any(
                "remaining_amount must be a non-negative exact decimal" in error
                for error in validate_spend_budget_evaluation_presentment(negative)
            )
        )
        mismatched = dict(instance, remaining_amount="10.00")
        self.assertIn(
            "$: remaining_amount and over_amount must match budget minus rated",
            validate_spend_budget_evaluation_presentment(mismatched),
        )
        wrong_status = dict(instance, utilization_status="over")
        self.assertIn(
            "$: utilization_status must match remaining and over",
            validate_spend_budget_evaluation_presentment(wrong_status),
        )
        waiting = dict(instance, next_operator_action="collect")
        self.assertIn(
            "$.next_operator_action: value is not in the allowed enumeration",
            validate_schema_instance(schema, waiting),
        )
        waiting_presentment = dict(instance)
        waiting_presentment["next_operator_action"] = "collect"
        self.assertIn(
            "$: published spend budget evaluations must wait",
            validate_spend_budget_evaluation_presentment(waiting_presentment),
        )
        self.assertNotEqual(validate_spend_budget_evaluation_presentment([]), ())
        bad_amount = dict(instance, rated_amount="not-decimal")
        self.assertTrue(
            any(
                "rated_amount must be an exact decimal" in error
                for error in validate_spend_budget_evaluation_presentment(bad_amount)
            )
        )
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: spend budget evaluation must not include card_pan",
            validate_spend_budget_evaluation_presentment(pan),
        )
        earnings = dict(instance, retained_earnings="310100")
        self.assertIn(
            "$: spend budget evaluation must not include retained_earnings",
            validate_spend_budget_evaluation_presentment(earnings),
        )
        self.assertNotEqual(
            validate_spend_budget_evaluation_presentment(dict(instance, budget_amount=100)),
            (),
        )

    def test_billing_account_budget_status_accepts_under_at_over_and_rejects_totals(self) -> None:
        """An account-level page keeps per-row exact math and never mixes currency."""
        schema = self._schema("billing-account-budget-status-presentment.schema.json")
        row = {
            "spend_budget_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "currency_code": "USD",
            "budget_amount": "100.00",
            "rated_amount": "40.00",
            "remaining_amount": "60.00",
            "over_amount": "0",
            "utilization_status": "under",
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "spend_budget_status": "published",
            "next_operator_action": "wait",
        }
        instance = {"budget_statuses": [row], "next_cursor": None}
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_billing_account_budget_status_presentment(instance), ())
        at_row = dict(row, rated_amount="100.00", remaining_amount="0", over_amount="0")
        at_row["utilization_status"] = "at"
        self.assertEqual(
            validate_billing_account_budget_status_presentment(
                {"budget_statuses": [at_row], "next_cursor": "2026-08-18T15:00:00Z|019d7b92-1aa0-7a7f-b61c-962c0f4bf680"}
            ),
            (),
        )
        over_row = dict(row, rated_amount="125.00", remaining_amount="0", over_amount="25.00")
        over_row["utilization_status"] = "over"
        self.assertEqual(
            validate_billing_account_budget_status_presentment(
                {"budget_statuses": [over_row], "next_cursor": None}
            ),
            (),
        )
        extra = dict(instance, items=[])
        self.assertIn(
            "$: additional property is not allowed: items",
            validate_schema_instance(schema, extra),
        )
        mixed = dict(instance, rated_amount="40.00")
        self.assertIn(
            "$: budget status page must not mix currencies into one rated_amount",
            validate_billing_account_budget_status_presentment(mixed),
        )
        zeroed = {"budget_statuses": [dict(row, budget_amount="0")], "next_cursor": None}
        self.assertTrue(
            any(
                "budget_amount must be greater than zero" in error
                for error in validate_billing_account_budget_status_presentment(zeroed)
            )
        )
        negative = {"budget_statuses": [dict(row, remaining_amount="-1")], "next_cursor": None}
        self.assertTrue(
            any(
                "remaining_amount must be a non-negative exact decimal" in error
                for error in validate_billing_account_budget_status_presentment(negative)
            )
        )
        mismatched = {"budget_statuses": [dict(row, remaining_amount="10.00")], "next_cursor": None}
        self.assertTrue(
            any(
                "remaining_amount and over_amount must match budget minus rated" in error
                for error in validate_billing_account_budget_status_presentment(mismatched)
            )
        )
        wrong_status = {"budget_statuses": [dict(row, utilization_status="over")], "next_cursor": None}
        self.assertTrue(
            any(
                "utilization_status must match remaining and over" in error
                for error in validate_billing_account_budget_status_presentment(wrong_status)
            )
        )
        waiting = {"budget_statuses": [dict(row, next_operator_action="collect")], "next_cursor": None}
        self.assertTrue(
            any(
                "published spend budget statuses must wait" in error
                for error in validate_billing_account_budget_status_presentment(waiting)
            )
        )
        self.assertNotEqual(validate_billing_account_budget_status_presentment([]), ())
        bad_amount = {"budget_statuses": [dict(row, rated_amount="not-decimal")], "next_cursor": None}
        self.assertTrue(
            any(
                "rated_amount must be an exact decimal" in error
                for error in validate_billing_account_budget_status_presentment(bad_amount)
            )
        )
        pan = dict(instance, card_pan="4111111111111111")
        self.assertIn(
            "$: spend budget status must not include card_pan",
            validate_billing_account_budget_status_presentment(pan),
        )
        earnings = dict(instance, retained_earnings="310100")
        self.assertIn(
            "$: spend budget status must not include retained_earnings",
            validate_billing_account_budget_status_presentment(earnings),
        )
        row_pan = {"budget_statuses": [dict(row, card_pan="4111111111111111")], "next_cursor": None}
        self.assertTrue(
            any(
                "spend budget status must not include card_pan" in error
                for error in validate_billing_account_budget_status_presentment(row_pan)
            )
        )
        row_earnings = {
            "budget_statuses": [dict(row, retained_earnings="310100")],
            "next_cursor": None,
        }
        self.assertTrue(
            any(
                "spend budget status must not include retained_earnings" in error
                for error in validate_billing_account_budget_status_presentment(row_earnings)
            )
        )
        skipped_row = {"budget_statuses": [row, "skip"], "next_cursor": None}
        self.assertNotEqual(validate_billing_account_budget_status_presentment(skipped_row), ())
        self.assertNotEqual(
            validate_billing_account_budget_status_presentment(
                {"budget_statuses": [dict(row, budget_amount=100)], "next_cursor": None}
            ),
            (),
        )
        self.assertNotEqual(
            validate_billing_account_budget_status_presentment(
                {"budget_statuses": "nope", "next_cursor": None}
            ),
            (),
        )

    def test_spend_budget_migration_is_tenant_scoped_and_append_only(self) -> None:
        """Spend-budget rows stay tenant-scoped and append-only."""
        sql = (ROOT / "database/migrations/0035_spend_budget.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.spend_budget",
            "UNIQUE (",
            "tenant_account_id",
            "billing_account_id",
            "window_started_at",
            "window_ended_at",
            "currency_code",
            "source_payload_hash",
            "spend_budget_contract_version",
            "UNIQUE (tenant_account_id, spend_budget_id)",
            "FOREIGN KEY (tenant_account_id, billing_account_id)",
            "budget_amount numeric(38, 12) NOT NULL CHECK (budget_amount > 0)",
            "CHECK (window_ended_at > window_started_at)",
        ):
            self.assertIn(expected_fragment, sql)
        status_sql = (ROOT / "database/migrations/0039_spend_budget_status.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ALTER TABLE billing_core.spend_budget",
            "ADD COLUMN spend_budget_status",
            "CHECK (spend_budget_status = 'published')",
        ):
            self.assertIn(expected_fragment, status_sql)

    def test_rate_card_presentment_accepts_lines_and_rejects_outcome(self) -> None:
        """A rate-card statement records exact unit prices and cannot claim a write outcome."""
        schema = self._schema("rate-card-presentment.schema.json")
        instance = {
            "rate_card_presentment_contract_version": 1,
            "rate_card_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf670",
            "tenant_reference": "urn:cwl:tenant_001",
            "rate_card_name": "cwl_standard",
            "currency_code": "USD",
            "rate_card_version": 1,
            "rate_card_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf671",
            "created_at": "2026-08-17T21:00:00Z",
            "published_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "rate_window",
            "lines": [
                {
                    "metric_code": "gen_ai_output_token",
                    "unit_amount": "0.000002",
                    "currency_code": "USD",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_rate_card_presentment(instance), ())
        posted = dict(instance)
        posted["rate_card_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: rate_card_outcome_code",
            validate_schema_instance(schema, posted),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: published cards must rate a window",
            validate_rate_card_presentment(wait_action),
        )
        self.assertNotEqual(validate_rate_card_presentment([]), ())
        missing_lines = dict(instance)
        missing_lines.pop("lines")
        self.assertNotEqual(validate_rate_card_presentment(missing_lines), ())
        zero = dict(instance)
        zero["lines"] = [
            {
                "metric_code": "gen_ai_output_token",
                "unit_amount": "0",
                "currency_code": "USD",
            }
        ]
        self.assertIn(
            "$: lines[0].unit_amount must be greater than zero",
            validate_rate_card_presentment(zero),
        )
        bad_unit = dict(instance)
        bad_unit["lines"] = [
            {
                "metric_code": "gen_ai_output_token",
                "unit_amount": "not-decimal",
                "currency_code": "USD",
            }
        ]
        self.assertTrue(
            any(
                "unit_amount must be an exact decimal" in error
                for error in validate_rate_card_presentment(bad_unit)
            )
        )
        not_object = dict(instance)
        not_object["lines"] = ["not-a-line"]
        self.assertIn(
            "$: lines[0] must be an object",
            validate_rate_card_presentment(not_object),
        )
        numeric = dict(instance)
        numeric["lines"] = [
            {
                "metric_code": "gen_ai_output_token",
                "unit_amount": 2,
                "currency_code": "USD",
            }
        ]
        self.assertEqual(
            [
                error
                for error in validate_rate_card_presentment(numeric)
                if "exact decimal" in error
            ],
            [],
        )

    def test_usage_event_presentment_accepts_quantity_and_rejects_outcome(self) -> None:
        """A usage-event statement records exact quantity and cannot claim a write outcome."""
        schema = self._schema("usage-event-presentment.schema.json")
        instance = {
            "usage_event_presentment_contract_version": 1,
            "usage_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf680",
            "tenant_reference": "urn:cwl:tenant_001",
            "source_event_key": "workflow_381:step_04:attempt_01",
            "event_payload_hash": "sha256:" + ("a" * 64),
            "occurred_at": "2026-08-16T10:27:42.482Z",
            "recorded_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "rate_window",
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
        self.assertEqual(validate_usage_event_presentment(instance), ())
        posted = dict(instance)
        posted["ingestion_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: ingestion_outcome_code",
            validate_schema_instance(schema, posted),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: stored usage must rate a window",
            validate_usage_event_presentment(wait_action),
        )
        self.assertNotEqual(validate_usage_event_presentment([]), ())
        missing_measurements = dict(instance)
        missing_measurements.pop("measurements")
        self.assertNotEqual(validate_usage_event_presentment(missing_measurements), ())
        negative = dict(instance)
        negative["measurements"] = [
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "-1",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ]
        self.assertIn(
            "$: measurements[0].quantity must not be negative",
            validate_usage_event_presentment(negative),
        )
        bad_quantity = dict(instance)
        bad_quantity["measurements"] = [
            {
                "meter_code": "gen_ai_output_token",
                "quantity": "not-decimal",
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ]
        self.assertTrue(
            any(
                "quantity must be an exact decimal" in error
                for error in validate_usage_event_presentment(bad_quantity)
            )
        )
        not_object = dict(instance)
        not_object["measurements"] = ["not-a-measurement"]
        self.assertIn(
            "$: measurements[0] must be an object",
            validate_usage_event_presentment(not_object),
        )
        numeric = dict(instance)
        numeric["measurements"] = [
            {
                "meter_code": "gen_ai_output_token",
                "quantity": 1810,
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ]
        self.assertEqual(
            [
                error
                for error in validate_usage_event_presentment(numeric)
                if "exact decimal" in error
            ],
            [],
        )

    def test_rating_run_presentment_accepts_total_and_rejects_outcome(self) -> None:
        """A rating-run statement records exact totals and cannot claim a write outcome."""
        schema = self._schema("rating-run-presentment.schema.json")
        instance = {
            "rating_run_presentment_contract_version": 1,
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf690",
            "tenant_reference": "urn:cwl:tenant_001",
            "rate_card_code": "cwl_standard",
            "rate_card_version": 1,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "usage_snapshot_hash": "sha256:" + ("c" * 64),
            "currency_code": "USD",
            "rated_total_amount": "0.003705",
            "recorded_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "draft_invoice",
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
        self.assertEqual(validate_rating_run_presentment(instance), ())
        posted = dict(instance)
        posted["rating_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: rating_outcome_code",
            validate_schema_instance(schema, posted),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: stored rating must draft an invoice",
            validate_rating_run_presentment(wait_action),
        )
        self.assertNotEqual(validate_rating_run_presentment([]), ())
        missing_lines = dict(instance)
        missing_lines.pop("rating_lines")
        self.assertNotEqual(validate_rating_run_presentment(missing_lines), ())
        negative = dict(instance)
        negative["rated_total_amount"] = "-1"
        self.assertIn(
            "$: rated_total_amount must not be negative",
            validate_rating_run_presentment(negative),
        )
        bad_total = dict(instance)
        bad_total["rated_total_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "rated_total_amount must be an exact decimal" in error
                for error in validate_rating_run_presentment(bad_total)
            )
        )
        not_object = dict(instance)
        not_object["rating_lines"] = ["not-a-line"]
        self.assertIn(
            "$: rating_lines[0] must be an object",
            validate_rating_run_presentment(not_object),
        )
        negative_line = dict(instance)
        negative_line["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "-1",
                "unit_price_amount": "0.000002",
                "line_total_amount": "0.003705",
            }
        ]
        self.assertIn(
            "$: rating_lines[0].rated_quantity must not be negative",
            validate_rating_run_presentment(negative_line),
        )
        negative_price = dict(instance)
        negative_price["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "1852.5",
                "unit_price_amount": "-1",
                "line_total_amount": "0.003705",
            }
        ]
        self.assertIn(
            "$: rating_lines[0].unit_price_amount must not be negative",
            validate_rating_run_presentment(negative_price),
        )
        negative_line_total = dict(instance)
        negative_line_total["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "1852.5",
                "unit_price_amount": "0.000002",
                "line_total_amount": "-1",
            }
        ]
        self.assertIn(
            "$: rating_lines[0].line_total_amount must not be negative",
            validate_rating_run_presentment(negative_line_total),
        )
        bad_line = dict(instance)
        bad_line["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "not-decimal",
                "unit_price_amount": "0.000002",
                "line_total_amount": "0.003705",
            }
        ]
        self.assertTrue(
            any(
                "rated_quantity must be an exact decimal" in error
                for error in validate_rating_run_presentment(bad_line)
            )
        )
        bad_price = dict(instance)
        bad_price["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "1852.5",
                "unit_price_amount": "not-decimal",
                "line_total_amount": "0.003705",
            }
        ]
        self.assertTrue(
            any(
                "unit_price_amount must be an exact decimal" in error
                for error in validate_rating_run_presentment(bad_price)
            )
        )
        bad_line_total = dict(instance)
        bad_line_total["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": "1852.5",
                "unit_price_amount": "0.000002",
                "line_total_amount": "not-decimal",
            }
        ]
        self.assertTrue(
            any(
                "line_total_amount must be an exact decimal" in error
                for error in validate_rating_run_presentment(bad_line_total)
            )
        )
        numeric_total = dict(instance)
        numeric_total["rated_total_amount"] = 2
        self.assertEqual(
            [
                error
                for error in validate_rating_run_presentment(numeric_total)
                if "exact decimal" in error
            ],
            [],
        )
        numeric = dict(instance)
        numeric["rating_lines"] = [
            {
                "line_number": 1,
                "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                "meter_code": "gen_ai_output_token",
                "unit_code": "token",
                "rated_quantity": 1852.5,
                "unit_price_amount": "0.000002",
                "line_total_amount": "0.003705",
            }
        ]
        self.assertEqual(
            [
                error
                for error in validate_rating_run_presentment(numeric)
                if "exact decimal" in error
            ],
            [],
        )

    def test_tax_assessment_presentment_accepts_amounts_and_rejects_outcome(self) -> None:
        """A tax-assessment statement records exact amounts and cannot claim a write outcome."""
        schema = self._schema("tax-assessment-presentment.schema.json")
        instance = {
            "tax_assessment_presentment_contract_version": 1,
            "tax_assessment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf6a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf6b0",
            "tax_rate_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf6c0",
            "tax_rate_version": 1,
            "tax_code": "vat",
            "tax_rate": "0.10",
            "currency_code": "USD",
            "tax_exclusive_amount": "100.00",
            "tax_amount": "10.00",
            "tax_inclusive_amount": "110.00",
            "source_payload_hash": "sha256:" + ("d" * 64),
            "assessed_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "propose_journal",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_tax_assessment_presentment(instance), ())
        posted = dict(instance)
        posted["tax_assessment_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: tax_assessment_outcome_code",
            validate_schema_instance(schema, posted),
        )
        statused = dict(instance)
        statused["tax_assessment_status"] = "assessed"
        self.assertIn(
            "$: additional property is not allowed: tax_assessment_status",
            validate_schema_instance(schema, statused),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: stored tax must propose a journal",
            validate_tax_assessment_presentment(wait_action),
        )
        self.assertNotEqual(validate_tax_assessment_presentment([]), ())
        negative = dict(instance)
        negative["tax_inclusive_amount"] = "-1"
        self.assertIn(
            "$: tax_inclusive_amount must not be negative",
            validate_tax_assessment_presentment(negative),
        )
        bad_total = dict(instance)
        bad_total["tax_inclusive_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_inclusive_amount must be an exact decimal" in error
                for error in validate_tax_assessment_presentment(bad_total)
            )
        )
        negative_exclusive = dict(instance)
        negative_exclusive["tax_exclusive_amount"] = "-1"
        self.assertIn(
            "$: tax_exclusive_amount must not be negative",
            validate_tax_assessment_presentment(negative_exclusive),
        )
        negative_tax = dict(instance)
        negative_tax["tax_amount"] = "-1"
        self.assertIn(
            "$: tax_amount must not be negative",
            validate_tax_assessment_presentment(negative_tax),
        )
        negative_rate = dict(instance)
        negative_rate["tax_rate"] = "-1"
        self.assertIn(
            "$: tax_rate must not be negative",
            validate_tax_assessment_presentment(negative_rate),
        )
        bad_exclusive = dict(instance)
        bad_exclusive["tax_exclusive_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_exclusive_amount must be an exact decimal" in error
                for error in validate_tax_assessment_presentment(bad_exclusive)
            )
        )
        bad_tax = dict(instance)
        bad_tax["tax_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_amount must be an exact decimal" in error
                for error in validate_tax_assessment_presentment(bad_tax)
            )
        )
        bad_rate = dict(instance)
        bad_rate["tax_rate"] = "not-decimal"
        self.assertTrue(
            any(
                "tax_rate must be an exact decimal" in error
                for error in validate_tax_assessment_presentment(bad_rate)
            )
        )
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "111.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_tax_assessment_presentment(unbalanced),
        )
        numeric_inclusive = dict(instance)
        numeric_inclusive["tax_inclusive_amount"] = 110
        self.assertEqual(
            [
                error
                for error in validate_tax_assessment_presentment(numeric_inclusive)
                if "exact decimal" in error
            ],
            [],
        )
        numeric_exclusive = dict(instance)
        numeric_exclusive["tax_exclusive_amount"] = 100
        self.assertEqual(
            [
                error
                for error in validate_tax_assessment_presentment(numeric_exclusive)
                if "exact decimal" in error
            ],
            [],
        )
        numeric_tax = dict(instance)
        numeric_tax["tax_amount"] = 10
        self.assertEqual(
            [
                error
                for error in validate_tax_assessment_presentment(numeric_tax)
                if "exact decimal" in error
            ],
            [],
        )
        numeric_rate = dict(instance)
        numeric_rate["tax_rate"] = 0.1
        self.assertEqual(
            [
                error
                for error in validate_tax_assessment_presentment(numeric_rate)
                if "exact decimal" in error
            ],
            [],
        )

    def test_posting_receipt_observation_presentment_accepts_status_and_rejects_outcome(
        self,
    ) -> None:
        """An observation statement records AIS status and cannot claim a write outcome."""
        schema = self._schema("posting-receipt-observation-presentment.schema.json")
        instance = {
            "posting_receipt_observation_presentment_contract_version": 1,
            "posting_receipt_observation_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf7a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "source_proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf7b0",
            "idempotency_key": "urn:cwl:tenant_001:invoice:key",
            "receipt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf7c0",
            "receipt_contract_version": 1,
            "source_payload_hash": "sha256:" + ("f" * 64),
            "posting_status_code": "posted",
            "recorded_at": "2026-08-17T18:00:00Z",
            "observed_at": "2026-08-17T21:00:00Z",
            "posted_at": "2026-08-17T18:05:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_posting_receipt_observation_presentment(instance), ())
        posted = dict(instance)
        posted["posting_receipt_observation_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: posting_receipt_observation_outcome_code",
            validate_schema_instance(schema, posted),
        )
        flipped = dict(instance)
        flipped["proposal_status"] = "posted"
        self.assertIn(
            "$: additional property is not allowed: proposal_status",
            validate_schema_instance(schema, flipped),
        )
        self.assertIn(
            "$: observation presentment must not claim proposal_status",
            validate_posting_receipt_observation_presentment(flipped),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "post"
        self.assertIn(
            "$: stored observation must wait",
            validate_posting_receipt_observation_presentment(wait_action),
        )
        self.assertNotEqual(validate_posting_receipt_observation_presentment([]), ())
        invented_status = dict(instance)
        invented_status["posting_status_code"] = "billing_posted"
        self.assertIn(
            "$: posting_status_code must remain an AIS-owned receipt status",
            validate_posting_receipt_observation_presentment(invented_status),
        )
        numeric_status = dict(instance)
        numeric_status["posting_status_code"] = 1
        self.assertEqual(
            [
                error
                for error in validate_posting_receipt_observation_presentment(numeric_status)
                if "AIS-owned" in error
            ],
            [],
        )

    def test_webhook_delivery_presentment_accepts_outcome_and_rejects_secret(self) -> None:
        """A delivery statement records stored outcome and cannot leak a secret."""
        schema = self._schema("webhook-delivery-presentment.schema.json")
        instance = {
            "webhook_delivery_presentment_contract_version": 1,
            "delivery_attempt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "webhook_subscription_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8b0",
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8c0",
            "event_type_code": "journal_proposal.validated",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8d0",
            "attempt_number": 1,
            "http_status": 200,
            "attempted_at": "2026-08-17T21:00:00Z",
            "delivered_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_webhook_delivery_presentment(instance), ())
        leaked = dict(instance)
        leaked["webhook_secret"] = "cwlwhsec_must_not_leak"
        self.assertIn(
            "$: additional property is not allowed: webhook_secret",
            validate_schema_instance(schema, leaked),
        )
        self.assertIn(
            "$: delivery presentment must not include webhook_secret",
            validate_webhook_delivery_presentment(leaked),
        )
        invented = dict(instance)
        invented["delivery_status"] = "delivered"
        self.assertIn(
            "$: additional property is not allowed: delivery_status",
            validate_schema_instance(schema, invented),
        )
        self.assertIn(
            "$: delivery presentment must not include delivery_status",
            validate_webhook_delivery_presentment(invented),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "retry"
        self.assertIn(
            "$: next_operator_action must be wait or run_deliveries",
            validate_webhook_delivery_presentment(wait_action),
        )
        self.assertIn(
            "$: delivered attempt must wait",
            validate_webhook_delivery_presentment(wait_action),
        )
        failed = {
            "webhook_delivery_presentment_contract_version": 1,
            "delivery_attempt_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8e0",
            "tenant_reference": "urn:cwl:tenant_001",
            "webhook_subscription_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf8f0",
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf900",
            "event_type_code": "journal_proposal.validated",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf910",
            "attempt_number": 1,
            "failure_reason_code": "webhook_http_error",
            "attempted_at": "2026-08-17T22:00:00Z",
            "next_operator_action": "run_deliveries",
        }
        self.assertEqual(validate_webhook_delivery_presentment(failed), ())
        failed_wait = dict(failed)
        failed_wait["next_operator_action"] = "wait"
        self.assertIn(
            "$: failed attempt must run_deliveries",
            validate_webhook_delivery_presentment(failed_wait),
        )
        self.assertNotEqual(validate_webhook_delivery_presentment([]), ())

    def test_tenant_api_credential_presentment_accepts_metadata_and_rejects_secret(
        self,
    ) -> None:
        """A credential statement records prefix and status and cannot leak a secret."""
        schema = self._schema("tenant-api-credential-presentment.schema.json")
        instance = {
            "tenant_api_credential_presentment_contract_version": 1,
            "tenant_api_credential_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf9a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "credential_label": "operator_key",
            "credential_prefix": "cwlak_fake001",
            "credential_status": "active",
            "tenant_api_credential_contract_version": 1,
            "issued_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_tenant_api_credential_presentment(instance), ())
        leaked = dict(instance)
        leaked["api_credential_secret"] = "cwlak_must_not_leak"
        self.assertIn(
            "$: additional property is not allowed: api_credential_secret",
            validate_schema_instance(schema, leaked),
        )
        self.assertIn(
            "$: credential presentment must not include api_credential_secret",
            validate_tenant_api_credential_presentment(leaked),
        )
        hashed = dict(instance)
        hashed["credential_secret_hash"] = "hmac-sha256:" + ("a" * 64)
        self.assertIn(
            "$: credential presentment must not include credential_secret_hash",
            validate_tenant_api_credential_presentment(hashed),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "rotate"
        self.assertIn(
            "$: next_operator_action must be wait or issue",
            validate_tenant_api_credential_presentment(wait_action),
        )
        self.assertIn(
            "$: active credential must wait",
            validate_tenant_api_credential_presentment(wait_action),
        )
        revoked = {
            "tenant_api_credential_presentment_contract_version": 1,
            "tenant_api_credential_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf9b0",
            "tenant_reference": "urn:cwl:tenant_001",
            "credential_label": "operator_key",
            "credential_prefix": "cwlak_fake002",
            "credential_status": "revoked",
            "tenant_api_credential_contract_version": 1,
            "issued_at": "2026-08-17T21:00:00Z",
            "revoked_at": "2026-08-17T22:00:00Z",
            "next_operator_action": "issue",
        }
        self.assertEqual(validate_tenant_api_credential_presentment(revoked), ())
        revoked_wait = dict(revoked)
        revoked_wait["next_operator_action"] = "wait"
        self.assertIn(
            "$: revoked credential must issue",
            validate_tenant_api_credential_presentment(revoked_wait),
        )
        self.assertNotEqual(validate_tenant_api_credential_presentment([]), ())

    def test_webhook_subscription_presentment_accepts_metadata_and_rejects_secret(
        self,
    ) -> None:
        """A subscription statement records URL and status and cannot leak a secret."""
        schema = self._schema("webhook-subscription-presentment.schema.json")
        instance = {
            "webhook_subscription_presentment_contract_version": 1,
            "webhook_subscription_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfaa0",
            "tenant_reference": "urn:cwl:tenant_001",
            "callback_url": "https://hooks.example.test/cwl",
            "event_type_codes": ["journal_proposal.validated"],
            "subscription_status": "active",
            "webhook_subscription_contract_version": 1,
            "issued_at": "2026-08-17T21:00:00Z",
            "next_operator_action": "run_deliveries",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_webhook_subscription_presentment(instance), ())
        leaked = dict(instance)
        leaked["webhook_secret"] = "cwlwh_must_not_leak"
        self.assertIn(
            "$: additional property is not allowed: webhook_secret",
            validate_schema_instance(schema, leaked),
        )
        self.assertIn(
            "$: subscription presentment must not include webhook_secret",
            validate_webhook_subscription_presentment(leaked),
        )
        hashed = dict(instance)
        hashed["webhook_secret_hash"] = "hmac-sha256:" + ("a" * 64)
        self.assertIn(
            "$: subscription presentment must not include webhook_secret_hash",
            validate_webhook_subscription_presentment(hashed),
        )
        prefixed = dict(instance)
        prefixed["webhook_secret_prefix"] = "cwlwh_fake001"
        self.assertIn(
            "$: subscription presentment must not include webhook_secret_prefix",
            validate_webhook_subscription_presentment(prefixed),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: next_operator_action must be run_deliveries or register",
            validate_webhook_subscription_presentment(wait_action),
        )
        self.assertIn(
            "$: active subscription must run_deliveries",
            validate_webhook_subscription_presentment(wait_action),
        )
        revoked = {
            "webhook_subscription_presentment_contract_version": 1,
            "webhook_subscription_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfab0",
            "tenant_reference": "urn:cwl:tenant_001",
            "callback_url": "https://hooks.example.test/cwl-revoked",
            "event_type_codes": ["journal_proposal.validated"],
            "subscription_status": "revoked",
            "webhook_subscription_contract_version": 1,
            "issued_at": "2026-08-17T21:00:00Z",
            "revoked_at": "2026-08-17T22:00:00Z",
            "next_operator_action": "register",
        }
        self.assertEqual(validate_webhook_subscription_presentment(revoked), ())
        revoked_wait = dict(revoked)
        revoked_wait["next_operator_action"] = "run_deliveries"
        self.assertIn(
            "$: revoked subscription must register",
            validate_webhook_subscription_presentment(revoked_wait),
        )
        self.assertNotEqual(validate_webhook_subscription_presentment([]), ())

    def test_dunning_event_presentment_accepts_metadata_and_rejects_delivery(
        self,
    ) -> None:
        """A dunning statement records notice code and cannot invent a send channel."""
        schema = self._schema("dunning-event-presentment.schema.json")
        instance = {
            "dunning_event_presentment_contract_version": 1,
            "dunning_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfba0",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf631",
            "dunning_event_number": 1,
            "dunning_notice_code": "first_notice",
            "occurred_at": "2026-08-17T21:05:00Z",
            "next_operator_action": "collect",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_dunning_event_presentment(instance), ())
        leaked = dict(instance)
        leaked["recipient"] = "buyer@example.test"
        self.assertIn(
            "$: additional property is not allowed: recipient",
            validate_schema_instance(schema, leaked),
        )
        self.assertIn(
            "$: dunning presentment must not include recipient",
            validate_dunning_event_presentment(leaked),
        )
        sent = dict(instance)
        sent["delivery_status"] = "sent"
        self.assertIn(
            "$: dunning presentment must not include delivery_status",
            validate_dunning_event_presentment(sent),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "send"
        self.assertIn(
            "$: next_operator_action must be collect or wait",
            validate_dunning_event_presentment(wait_action),
        )
        settled = {
            "dunning_event_presentment_contract_version": 1,
            "dunning_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfbb0",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf631",
            "dunning_event_number": 1,
            "dunning_notice_code": "first_notice",
            "occurred_at": "2026-08-17T21:05:00Z",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_dunning_event_presentment(settled), ())
        self.assertNotEqual(validate_dunning_event_presentment([]), ())

    def test_webhook_outbox_event_presentment_accepts_metadata_and_rejects_body(
        self,
    ) -> None:
        """An outbox statement records metadata and cannot leak the signed body."""
        schema = self._schema("webhook-outbox-event-presentment.schema.json")
        instance = {
            "webhook_outbox_event_presentment_contract_version": 1,
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfca0",
            "tenant_reference": "urn:cwl:tenant_001",
            "event_type_code": "journal_proposal.validated",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfcb0",
            "payload_hash": "sha256:" + ("a" * 64),
            "occurred_at": "2026-08-17T21:00:00Z",
            "enqueued_at": "2026-08-17T21:00:00Z",
            "delivery_status": "pending",
            "attempted_delivery_count": 0,
            "next_operator_action": "run_deliveries",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_webhook_outbox_event_presentment(instance), ())
        leaked = dict(instance)
        leaked["payload_json"] = '{"secret_blob":"must-not-leak"}'
        self.assertIn(
            "$: additional property is not allowed: payload_json",
            validate_schema_instance(schema, leaked),
        )
        self.assertIn(
            "$: outbox presentment must not include payload_json",
            validate_webhook_outbox_event_presentment(leaked),
        )
        secret = dict(instance)
        secret["webhook_secret"] = "cwlwh_must-not-leak"
        self.assertIn(
            "$: outbox presentment must not include webhook_secret",
            validate_webhook_outbox_event_presentment(secret),
        )
        send_action = dict(instance)
        send_action["next_operator_action"] = "send"
        self.assertIn(
            "$: next_operator_action must be wait or run_deliveries",
            validate_webhook_outbox_event_presentment(send_action),
        )
        pending_wait = dict(instance)
        pending_wait["next_operator_action"] = "wait"
        self.assertIn(
            "$: pending outbox event must run_deliveries",
            validate_webhook_outbox_event_presentment(pending_wait),
        )
        delivered = {
            "webhook_outbox_event_presentment_contract_version": 1,
            "outbox_event_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfcc0",
            "tenant_reference": "urn:cwl:tenant_001",
            "event_type_code": "payment_receipt.applied",
            "source_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfcd0",
            "payload_hash": "sha256:" + ("b" * 64),
            "occurred_at": "2026-08-17T22:00:00Z",
            "enqueued_at": "2026-08-17T22:00:00Z",
            "delivery_status": "delivered",
            "attempted_delivery_count": 1,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_webhook_outbox_event_presentment(delivered), ())
        delivered_run = dict(delivered)
        delivered_run["next_operator_action"] = "run_deliveries"
        self.assertIn(
            "$: delivered outbox event must wait",
            validate_webhook_outbox_event_presentment(delivered_run),
        )
        self.assertNotEqual(validate_webhook_outbox_event_presentment([]), ())

    def test_issued_invoice_accepts_snapshot_and_rejects_numbering(self) -> None:
        """An issued-invoice contract freezes exact totals and cannot invent a number."""
        schema = self._schema("issued-invoice.schema.json")
        instance = {
            "issued_invoice_contract_version": 1,
            "issued_invoice_outcome_code": "accepted",
            "issued_invoice_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd10",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf631",
            "usage_snapshot_hash": "sha256:" + ("a" * 64),
            "currency_code": "USD",
            "tax_exclusive_amount": "0.003705",
            "tax_amount": "0",
            "tax_inclusive_amount": "0.003705",
            "issued_invoice_status": "issued",
            "issued_at": "2026-08-17T21:00:00Z",
            "source_payload_hash": "sha256:" + ("b" * 64),
            "idempotency_key": "urn:cwl:tenant_001:issued_invoice:id:hash:v1",
            "next_operator_action": "collect",
            "issued_invoice_lines": [
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
        self.assertEqual(validate_issued_invoice(instance), ())
        numbered = dict(instance)
        numbered["invoice_number"] = "INV-0001"
        self.assertIn(
            "$: additional property is not allowed: invoice_number",
            validate_schema_instance(schema, numbered),
        )
        self.assertIn(
            "$: issued invoice must not include invoice_number",
            validate_issued_invoice(numbered),
        )
        legal = dict(instance)
        legal["legal_invoice_number"] = "2026/0001"
        self.assertIn(
            "$: issued invoice must not include legal_invoice_number",
            validate_issued_invoice(legal),
        )
        pan = dict(instance)
        pan["card_pan"] = "4111111111111111"
        self.assertIn("$: issued invoice must not include card_pan", validate_issued_invoice(pan))
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn("$: issued invoice must collect", validate_issued_invoice(wait_action))
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "1.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_issued_invoice(unbalanced),
        )
        negative = dict(instance)
        negative["tax_exclusive_amount"] = "-1"
        self.assertIn(
            "$: tax_exclusive_amount must not be negative",
            validate_issued_invoice(negative),
        )
        bad_decimal = dict(instance)
        bad_decimal["tax_amount"] = "not-decimal"
        self.assertTrue(
            any("must be an exact decimal" in error for error in validate_issued_invoice(bad_decimal))
        )
        numeric = dict(instance)
        numeric["tax_amount"] = 0
        self.assertTrue(
            any("must be an exact decimal" in error for error in validate_issued_invoice(numeric))
        )
        rejected = {
            "issued_invoice_contract_version": 1,
            "issued_invoice_outcome_code": "rejected",
            "rejection_reason_code": "invoice_draft_not_found",
        }
        self.assertEqual(validate_issued_invoice(rejected), ())
        missing_reason = {
            "issued_invoice_contract_version": 1,
            "issued_invoice_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected issued invoices must include rejection_reason_code",
            validate_issued_invoice(missing_reason),
        )
        missing_id = {
            "issued_invoice_contract_version": 1,
            "issued_invoice_outcome_code": "accepted",
        }
        self.assertIn(
            "$: accepted issued invoices must include issued_invoice_id",
            validate_issued_invoice(missing_id),
        )
        self.assertNotEqual(validate_issued_invoice([]), ())
        self.assertNotEqual(
            validate_issued_invoice({"issued_invoice_contract_version": 1}),
            (),
        )

    def test_issued_invoice_presentment_accepts_snapshot_and_rejects_numbering(self) -> None:
        """An issued-invoice statement records frozen totals and cannot claim a write outcome."""
        schema = self._schema("issued-invoice-presentment.schema.json")
        instance = {
            "issued_invoice_presentment_contract_version": 1,
            "issued_invoice_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd10",
            "tenant_reference": "urn:cwl:tenant_001",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "rating_run_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf631",
            "usage_snapshot_hash": "sha256:" + ("a" * 64),
            "currency_code": "USD",
            "tax_exclusive_amount": "100.00",
            "tax_amount": "10.00",
            "tax_inclusive_amount": "110.00",
            "issued_invoice_status": "issued",
            "issued_at": "2026-08-17T21:00:00Z",
            "due_at": "2026-09-16T21:00:00Z",
            "source_payload_hash": "sha256:" + ("b" * 64),
            "issued_invoice_contract_version": 1,
            "next_operator_action": "collect",
            "issued_invoice_lines": [
                {
                    "line_number": 1,
                    "billing_account_reference": "urn:cwl:tenant_001:billing_account:019d7001",
                    "meter_code": "gen_ai_output_token",
                    "unit_code": "token",
                    "rated_quantity": "50000",
                    "unit_price_amount": "0.002",
                    "line_total_amount": "100.00",
                }
            ],
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_issued_invoice_presentment(instance), ())
        posted = dict(instance)
        posted["issued_invoice_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: issued_invoice_outcome_code",
            validate_schema_instance(schema, posted),
        )
        numbered = dict(instance)
        numbered["invoice_number"] = "INV-0001"
        self.assertIn(
            "$: issued invoice must not include invoice_number",
            validate_issued_invoice_presentment(numbered),
        )
        wait_action = dict(instance)
        wait_action["next_operator_action"] = "wait"
        self.assertIn(
            "$: stored issued invoice must collect",
            validate_issued_invoice_presentment(wait_action),
        )
        negative = dict(instance)
        negative["tax_amount"] = "-1"
        self.assertIn(
            "$: tax_amount must not be negative",
            validate_issued_invoice_presentment(negative),
        )
        numeric = dict(instance)
        numeric["tax_inclusive_amount"] = 110.0
        self.assertTrue(
            any(
                "must be an exact decimal" in error
                for error in validate_issued_invoice_presentment(numeric)
            )
        )
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "999.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_issued_invoice_presentment(unbalanced),
        )
        self.assertNotEqual(validate_issued_invoice_presentment([]), ())

    def test_issued_credit_note_accepts_snapshot_and_rejects_numbering(self) -> None:
        """An issued credit note freezes credit totals and cannot invent a legal number."""
        schema = self._schema("issued-credit-note.schema.json")
        instance = {
            "issued_credit_note_contract_version": 1,
            "issued_credit_note_outcome_code": "accepted",
            "issued_credit_note_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd20",
            "tenant_reference": "urn:cwl:tenant_001",
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "tax_exclusive_amount": "0.003705",
            "tax_amount": "0",
            "tax_inclusive_amount": "0.003705",
            "issued_credit_note_status": "issued",
            "issued_at": "2026-08-17T21:00:00Z",
            "source_payload_hash": "sha256:" + ("b" * 64),
            "credit_adjustment_source_payload_hash": "sha256:" + ("c" * 64),
            "credit_adjustment_contract_version": 1,
            "credit_reason_code": "rating_correction",
            "idempotency_key": "urn:cwl:tenant_001:issued_credit_note:id:hash:v1",
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_issued_credit_note(instance), ())
        numbered = dict(instance)
        numbered["credit_note_number"] = "CN-0001"
        self.assertIn(
            "$: additional property is not allowed: credit_note_number",
            validate_schema_instance(schema, numbered),
        )
        self.assertIn(
            "$: issued credit note must not include credit_note_number",
            validate_issued_credit_note(numbered),
        )
        legal = dict(instance)
        legal["legal_credit_note_number"] = "2026/0001"
        self.assertIn(
            "$: issued credit note must not include legal_credit_note_number",
            validate_issued_credit_note(legal),
        )
        pan = dict(instance)
        pan["card_pan"] = "4111111111111111"
        self.assertIn(
            "$: issued credit note must not include card_pan",
            validate_issued_credit_note(pan),
        )
        collect_action = dict(instance)
        collect_action["next_operator_action"] = "collect"
        self.assertIn("$: issued credit note must wait", validate_issued_credit_note(collect_action))
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "1.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_issued_credit_note(unbalanced),
        )
        negative = dict(instance)
        negative["tax_exclusive_amount"] = "-1"
        self.assertIn(
            "$: tax_exclusive_amount must not be negative",
            validate_issued_credit_note(negative),
        )
        bad_decimal = dict(instance)
        bad_decimal["tax_amount"] = "not-decimal"
        self.assertTrue(
            any(
                "must be an exact decimal" in error
                for error in validate_issued_credit_note(bad_decimal)
            )
        )
        numeric = dict(instance)
        numeric["tax_amount"] = 0
        self.assertTrue(
            any("must be an exact decimal" in error for error in validate_issued_credit_note(numeric))
        )
        rejected = {
            "issued_credit_note_contract_version": 1,
            "issued_credit_note_outcome_code": "rejected",
            "rejection_reason_code": "credit_adjustment_not_found",
        }
        self.assertEqual(validate_issued_credit_note(rejected), ())
        missing_reason = {
            "issued_credit_note_contract_version": 1,
            "issued_credit_note_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected issued credit notes must include rejection_reason_code",
            validate_issued_credit_note(missing_reason),
        )
        missing_id = {
            "issued_credit_note_contract_version": 1,
            "issued_credit_note_outcome_code": "accepted",
        }
        self.assertIn(
            "$: accepted issued credit notes must include issued_credit_note_id",
            validate_issued_credit_note(missing_id),
        )
        self.assertNotEqual(validate_issued_credit_note([]), ())
        self.assertNotEqual(
            validate_issued_credit_note({"issued_credit_note_contract_version": 1}),
            (),
        )

    def test_issued_credit_note_presentment_accepts_snapshot_and_rejects_numbering(self) -> None:
        """An issued-credit-note statement records frozen totals and cannot claim a write outcome."""
        schema = self._schema("issued-credit-note-presentment.schema.json")
        instance = {
            "issued_credit_note_presentment_contract_version": 1,
            "issued_credit_note_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd20",
            "tenant_reference": "urn:cwl:tenant_001",
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "issued_invoice_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd10",
            "currency_code": "USD",
            "tax_exclusive_amount": "10.00",
            "tax_amount": "1.00",
            "tax_inclusive_amount": "11.00",
            "issued_credit_note_status": "issued",
            "issued_at": "2026-08-17T21:00:00Z",
            "source_payload_hash": "sha256:" + ("b" * 64),
            "credit_adjustment_source_payload_hash": "sha256:" + ("c" * 64),
            "credit_adjustment_contract_version": 1,
            "credit_reason_code": "goodwill",
            "issued_credit_note_contract_version": 1,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_issued_credit_note_presentment(instance), ())
        posted = dict(instance)
        posted["issued_credit_note_outcome_code"] = "accepted"
        self.assertIn(
            "$: additional property is not allowed: issued_credit_note_outcome_code",
            validate_schema_instance(schema, posted),
        )
        numbered = dict(instance)
        numbered["credit_note_number"] = "CN-0001"
        self.assertIn(
            "$: issued credit note must not include credit_note_number",
            validate_issued_credit_note_presentment(numbered),
        )
        collect_action = dict(instance)
        collect_action["next_operator_action"] = "collect"
        self.assertIn(
            "$: stored issued credit note must wait",
            validate_issued_credit_note_presentment(collect_action),
        )
        negative = dict(instance)
        negative["tax_amount"] = "-1"
        self.assertIn(
            "$: tax_amount must not be negative",
            validate_issued_credit_note_presentment(negative),
        )
        numeric = dict(instance)
        numeric["tax_inclusive_amount"] = 11.0
        self.assertTrue(
            any(
                "must be an exact decimal" in error
                for error in validate_issued_credit_note_presentment(numeric)
            )
        )
        unbalanced = dict(instance)
        unbalanced["tax_inclusive_amount"] = "999.00"
        self.assertIn(
            "$: tax_inclusive_amount must equal exclusive plus tax",
            validate_issued_credit_note_presentment(unbalanced),
        )
        self.assertNotEqual(validate_issued_credit_note_presentment([]), ())

    def test_tenant_api_credential_accepts_issue_secret_and_rejects_hash(self) -> None:
        """Issue may return the secret once; hashes and rejected secrets are forbidden."""
        schema = self._schema("tenant-api-credential.schema.json")
        instance = {
            "tenant_api_credential_contract_version": 1,
            "tenant_api_credential_outcome_code": "accepted",
            "tenant_api_credential_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf6a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "credential_label": "operator_key",
            "credential_prefix": "cwlak_xxxxxx",
            "api_credential_secret": "cwlak_" + ("x" * 32),
            "credential_status": "active",
            "issued_at": "2026-08-17T22:00:00Z",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_tenant_api_credential(instance), ())
        hashed = dict(instance)
        hashed["credential_secret_hash"] = "hmac-sha256:" + ("a" * 64)
        self.assertIn(
            "$: additional property is not allowed: credential_secret_hash",
            validate_schema_instance(schema, hashed),
        )
        self.assertIn(
            "$: persisted hashes must not appear on the HTTP contract",
            validate_tenant_api_credential(hashed),
        )
        rejected = {
            "tenant_api_credential_contract_version": 1,
            "tenant_api_credential_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_schema_instance(schema, rejected), ())
        leaked = dict(rejected)
        leaked["api_credential_secret"] = "should-not-be-here"
        self.assertIn(
            "$: rejected credentials must not include api_credential_secret",
            validate_tenant_api_credential(leaked),
        )
        missing_reason = {
            "tenant_api_credential_contract_version": 1,
            "tenant_api_credential_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected credentials must include rejection_reason_code",
            validate_tenant_api_credential(missing_reason),
        )
        self.assertNotEqual(validate_tenant_api_credential([]), ())
        accepted_missing = {
            "tenant_api_credential_contract_version": 1,
            "tenant_api_credential_outcome_code": "accepted",
        }
        self.assertTrue(
            any("must include" in error for error in validate_tenant_api_credential(accepted_missing))
        )
        unknown_outcome = {
            "tenant_api_credential_contract_version": 1,
            "tenant_api_credential_outcome_code": "posted",
        }
        self.assertTrue(validate_tenant_api_credential(unknown_outcome))
        replay = dict(instance)
        replay["tenant_api_credential_outcome_code"] = "duplicate_replay"
        del replay["api_credential_secret"]
        self.assertEqual(validate_tenant_api_credential(replay), ())

    def test_webhook_subscription_accepts_register_secret_and_rejects_hash(self) -> None:
        """Register may return the secret once; hashes and rejected secrets are forbidden."""
        schema = self._schema("webhook-subscription.schema.json")
        instance = {
            "webhook_subscription_contract_version": 1,
            "webhook_subscription_outcome_code": "accepted",
            "webhook_subscription_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf6a0",
            "tenant_reference": "urn:cwl:tenant_001",
            "callback_url": "https://hooks.example.test/cwl",
            "event_type_codes": ["journal_proposal.validated"],
            "webhook_secret_prefix": "cwlwh_xxxxxx",
            "webhook_secret": "cwlwh_" + ("x" * 32),
            "subscription_status": "active",
            "issued_at": "2026-08-17T22:00:00Z",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_webhook_subscription(instance), ())
        hashed = dict(instance)
        hashed["webhook_secret_hash"] = "hmac-sha256:" + ("a" * 64)
        self.assertIn(
            "$: additional property is not allowed: webhook_secret_hash",
            validate_schema_instance(schema, hashed),
        )
        self.assertIn(
            "$: persisted hashes must not appear on the HTTP contract",
            validate_webhook_subscription(hashed),
        )
        rejected = {
            "webhook_subscription_contract_version": 1,
            "webhook_subscription_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_schema_instance(schema, rejected), ())
        leaked = dict(rejected)
        leaked["webhook_secret"] = "should-not-be-here"
        self.assertIn(
            "$: rejected subscriptions must not include webhook_secret",
            validate_webhook_subscription(leaked),
        )
        missing_reason = {
            "webhook_subscription_contract_version": 1,
            "webhook_subscription_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected subscriptions must include rejection_reason_code",
            validate_webhook_subscription(missing_reason),
        )
        self.assertNotEqual(validate_webhook_subscription([]), ())
        accepted_missing = {
            "webhook_subscription_contract_version": 1,
            "webhook_subscription_outcome_code": "accepted",
        }
        self.assertTrue(
            any("must include" in error for error in validate_webhook_subscription(accepted_missing))
        )
        replay = dict(instance)
        replay["webhook_subscription_outcome_code"] = "duplicate_replay"
        self.assertIn(
            "$: replayed subscriptions must not include webhook_secret",
            validate_webhook_subscription(replay),
        )
        del replay["webhook_secret"]
        self.assertEqual(validate_webhook_subscription(replay), ())
        delivery = {
            "webhook_delivery_contract_version": 1,
            "webhook_delivery_outcome_code": "accepted",
            "delivered_event_count": 1,
            "attempted_delivery_count": 1,
            "failed_delivery_count": 0,
        }
        self.assertEqual(validate_webhook_delivery(delivery), ())
        self.assertNotEqual(validate_webhook_delivery([]), ())
        missing_counts = {
            "webhook_delivery_contract_version": 1,
            "webhook_delivery_outcome_code": "accepted",
        }
        self.assertTrue(
            any("must include" in error for error in validate_webhook_delivery(missing_counts))
        )
        rejected_delivery = {
            "webhook_delivery_contract_version": 1,
            "webhook_delivery_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_webhook_delivery(rejected_delivery), ())
        missing_delivery_reason = {
            "webhook_delivery_contract_version": 1,
            "webhook_delivery_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected deliveries must include rejection_reason_code",
            validate_webhook_delivery(missing_delivery_reason),
        )
        unknown_subscription = {
            "webhook_subscription_contract_version": 1,
            "webhook_subscription_outcome_code": "posted",
        }
        self.assertTrue(validate_webhook_subscription(unknown_subscription))
        unknown_delivery = {
            "webhook_delivery_contract_version": 1,
            "webhook_delivery_outcome_code": "posted",
        }
        self.assertTrue(validate_webhook_delivery(unknown_delivery))
        drain = {
            "ais_outbox_drain_contract_version": 1,
            "ais_outbox_drain_outcome_code": "accepted",
            "outbox_event_count": 0,
            "receipt_lookup_count": 0,
            "observed_receipt_count": 0,
            "published_event_count": 0,
            "skipped_event_count": 0,
            "next_cursor": None,
        }
        self.assertEqual(validate_ais_outbox_drain(drain), ())
        self.assertNotEqual(validate_ais_outbox_drain([]), ())
        missing_drain_counts = {
            "ais_outbox_drain_contract_version": 1,
            "ais_outbox_drain_outcome_code": "accepted",
        }
        self.assertTrue(
            any("must include" in error for error in validate_ais_outbox_drain(missing_drain_counts))
        )
        rejected_drain = {
            "ais_outbox_drain_contract_version": 1,
            "ais_outbox_drain_outcome_code": "rejected",
            "rejection_reason_code": "tenant_not_found",
        }
        self.assertEqual(validate_ais_outbox_drain(rejected_drain), ())
        missing_drain_reason = {
            "ais_outbox_drain_contract_version": 1,
            "ais_outbox_drain_outcome_code": "rejected",
        }
        self.assertIn(
            "$: rejected drains must include rejection_reason_code",
            validate_ais_outbox_drain(missing_drain_reason),
        )
        unknown_drain = {
            "ais_outbox_drain_contract_version": 1,
            "ais_outbox_drain_outcome_code": "posted",
        }
        self.assertTrue(validate_ais_outbox_drain(unknown_drain))

    def test_webhook_outbox_migration_stores_keyed_hash(self) -> None:
        """Webhook tables persist a keyed HMAC and append-only delivery attempts."""
        sql = (ROOT / "database/migrations/0016_webhook_outbox.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.webhook_subscription",
            "CREATE TABLE billing_core.webhook_outbox_event",
            "CREATE TABLE billing_core.webhook_delivery_attempt",
            "webhook_secret_hash text NOT NULL CHECK (webhook_secret_hash ~ '^hmac-sha256:[0-9a-f]{64}$')",
            "subscription_status text NOT NULL CHECK (subscription_status IN ('active', 'revoked'))",
            "UNIQUE (tenant_account_id, callback_url, event_type_set, webhook_subscription_contract_version)",
            "UNIQUE (tenant_account_id, event_type_code, source_id, payload_hash)",
            "UNIQUE (outbox_event_id, webhook_subscription_id, attempt_number)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_tenant_api_credential_migration_stores_keyed_hash(self) -> None:
        """API credentials persist a keyed HMAC and never a recoverable secret."""
        sql = (ROOT / "database/migrations/0015_tenant_api_credential.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.tenant_api_credential",
            "credential_secret_hash text NOT NULL CHECK (credential_secret_hash ~ '^hmac-sha256:[0-9a-f]{64}$')",
            "credential_status text NOT NULL CHECK (credential_status IN ('active', 'revoked'))",
            "UNIQUE (credential_secret_hash)",
            "UNIQUE (tenant_account_id, tenant_api_credential_id)",
        ):
            self.assertIn(expected_fragment, sql)

    def test_issued_invoice_migration_persists_append_only_snapshots(self) -> None:
        """The issued-invoice migration must stay tenant-scoped and number-free."""
        sql = (ROOT / "database/migrations/0017_issued_invoice.sql").read_text(encoding="utf-8")
        for expected_fragment in (
            "CREATE TABLE billing_core.issued_invoice",
            "CREATE TABLE billing_core.issued_invoice_line",
            "UNIQUE (tenant_account_id, invoice_draft_id)",
            "UNIQUE (tenant_account_id, issued_invoice_id)",
            "FOREIGN KEY (tenant_account_id, invoice_draft_id)",
            "FOREIGN KEY (tenant_account_id, issued_invoice_id)",
            "issued_invoice_status text NOT NULL CHECK (issued_invoice_status IN ('issued'))",
            "CHECK (source_payload_hash ~ '^sha256:[0-9a-f]{64}$')",
            "CHECK (tax_inclusive_amount = tax_exclusive_amount + tax_amount)",
        ):
            self.assertIn(expected_fragment, sql)
        self.assertNotIn("invoice_number", sql)
        self.assertNotIn("legal_invoice_number", sql)

    def test_issued_invoice_void_accepts_recorded_void_and_rejects_numbering(self) -> None:
        """A void contract records exact amount and cannot invent a legal number."""
        schema = self._schema("issued-invoice-void.schema.json")
        instance = {
            "issued_invoice_void_contract_version": 1,
            "issued_invoice_void_outcome_code": "accepted",
            "issued_invoice_void_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd80",
            "tenant_reference": "urn:cwl:tenant_001",
            "issued_invoice_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd10",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf630",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "currency_code": "USD",
            "voided_amount": "0.003705",
            "remaining_outstanding_amount": "0",
            "issued_invoice_void_status": "recorded",
            "collection_case_status": "voided",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": "sha256:" + ("d" * 64),
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_issued_invoice_void(instance), ())
        presentment = {
            "issued_invoice_void_presentment_contract_version": 1,
            "issued_invoice_void_id": instance["issued_invoice_void_id"],
            "tenant_reference": instance["tenant_reference"],
            "issued_invoice_id": instance["issued_invoice_id"],
            "invoice_draft_id": instance["invoice_draft_id"],
            "collection_case_id": instance["collection_case_id"],
            "currency_code": "USD",
            "voided_amount": "0.003705",
            "remaining_outstanding_amount": "0",
            "issued_invoice_void_status": "recorded",
            "collection_case_status": "voided",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": instance["source_payload_hash"],
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_issued_invoice_void_presentment(presentment), ())
        numbered = dict(instance)
        numbered["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_issued_invoice_void(numbered))

    def test_issued_invoice_void_migration_persists_append_only_voids(self) -> None:
        """The void migration must stay tenant-scoped and must not reuse settled."""
        sql = (ROOT / "database/migrations/0029_issued_invoice_void.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.issued_invoice_void",
            "UNIQUE (tenant_account_id, issued_invoice_id)",
            "UNIQUE (tenant_account_id, issued_invoice_void_id)",
            "FOREIGN KEY (tenant_account_id, issued_invoice_id)",
            "issued_invoice_void_status text NOT NULL CHECK (\n        issued_invoice_void_status IN ('recorded')",
            "CHECK (collection_case_status IN ('open', 'dunning', 'settled', 'voided'))",
            "collection_case_status IN ('settled', 'voided')",
            "remaining_outstanding_amount = 0",
            "CHECK (voided_amount > 0)",
        ):
            self.assertIn(expected_fragment, sql)
        self.assertNotIn("legal_invoice_number", sql)

    def test_issued_credit_note_void_accepts_recorded_void_and_rejects_numbering(self) -> None:
        """A credit-note void contract records exact amount and cannot invent a legal number."""
        schema = self._schema("issued-credit-note-void.schema.json")
        instance = {
            "issued_credit_note_void_contract_version": 1,
            "issued_credit_note_void_outcome_code": "accepted",
            "issued_credit_note_void_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd90",
            "tenant_reference": "urn:cwl:tenant_001",
            "issued_credit_note_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd91",
            "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd92",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "voided_amount": "0.003705",
            "issued_credit_note_void_status": "recorded",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": "sha256:" + ("e" * 64),
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        self.assertEqual(validate_issued_credit_note_void(instance), ())
        presentment = {
            "issued_credit_note_void_presentment_contract_version": 1,
            "issued_credit_note_void_id": instance["issued_credit_note_void_id"],
            "tenant_reference": instance["tenant_reference"],
            "issued_credit_note_id": instance["issued_credit_note_id"],
            "credit_adjustment_id": instance["credit_adjustment_id"],
            "invoice_draft_id": instance["invoice_draft_id"],
            "currency_code": "USD",
            "voided_amount": "0.003705",
            "issued_credit_note_void_status": "recorded",
            "voided_at": "2026-08-18T12:00:00Z",
            "source_payload_hash": instance["source_payload_hash"],
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_issued_credit_note_void_presentment(presentment), ())
        numbered = dict(instance)
        numbered["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_issued_credit_note_void(numbered))

    def test_issued_credit_note_void_migration_persists_append_only_voids(self) -> None:
        """The credit-note void migration must stay tenant-scoped and unused-only."""
        sql = (ROOT / "database/migrations/0033_issued_credit_note_void.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.issued_credit_note_void",
            "UNIQUE (tenant_account_id, issued_credit_note_id)",
            "UNIQUE (tenant_account_id, issued_credit_note_void_id)",
            "FOREIGN KEY (tenant_account_id, issued_credit_note_id)",
            "issued_credit_note_void_status text NOT NULL CHECK (\n        issued_credit_note_void_status IN ('recorded')",
            "CHECK (voided_amount > 0)",
        ):
            self.assertIn(expected_fragment, sql)
        self.assertNotIn("legal_credit_note_number", sql)
        self.assertNotIn("remaining_outstanding_amount", sql)

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
        voided = dict(instance, collection_case_status="voided", outstanding_amount="0")
        self.assertEqual(validate_schema_instance(schema, voided), ())
        disputed = dict(instance, collection_case_status="disputed")
        self.assertEqual(validate_schema_instance(schema, disputed), ())

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

    def test_collection_dispute_accepts_held_status_and_closed_reasons(self) -> None:
        """A collection-dispute contract records exact remaining and held-only status."""
        schema = self._schema("collection-dispute.schema.json")
        instance = {
            "collection_dispute_contract_version": 1,
            "collection_dispute_outcome_code": "accepted",
            "collection_dispute_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd70",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "remaining_outstanding_amount": "0.003705",
            "collection_dispute_status": "held",
            "collection_case_status": "disputed",
            "held_at": "2026-08-18T13:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        released = dict(instance, collection_dispute_status="released")
        self.assertIn(
            "$.collection_dispute_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, released),
        )

    def test_collection_dispute_migration_persists_append_only_holds(self) -> None:
        """The dispute-hold migration stays tenant-scoped and money-neutral."""
        sql = (ROOT / "database/migrations/0031_collection_dispute.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "CREATE TABLE billing_core.collection_dispute",
            "UNIQUE (tenant_account_id, collection_case_id)",
            "UNIQUE (tenant_account_id, collection_dispute_id)",
            "FOREIGN KEY (tenant_account_id, collection_case_id)",
            "collection_dispute_status text NOT NULL CHECK (",
            "collection_dispute_status IN ('held')",
            "collection_case_status IN ('open', 'dunning', 'settled', 'voided', 'disputed')",
            "OR collection_case_status IN ('open', 'dunning', 'disputed')",
        ):
            self.assertIn(expected_fragment, sql)

    def test_collection_dispute_release_accepts_released_status(self) -> None:
        """A collection-dispute-release contract records exact remaining and released status."""
        schema = self._schema("collection-dispute-release.schema.json")
        instance = {
            "collection_dispute_release_contract_version": 1,
            "collection_dispute_release_outcome_code": "accepted",
            "collection_dispute_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd70",
            "tenant_reference": "urn:cwl:tenant_001",
            "collection_case_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bfd21",
            "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
            "currency_code": "USD",
            "remaining_outstanding_amount": "0.003705",
            "collection_dispute_status": "released",
            "collection_case_status": "open",
            "released_at": "2026-08-18T15:00:00Z",
            "source_payload_hash": "sha256:" + "d" * 64,
            "next_operator_action": "wait",
        }
        self.assertEqual(validate_schema_instance(schema, instance), ())
        held = dict(instance, collection_dispute_status="held")
        self.assertIn(
            "$.collection_dispute_status: value is not in the allowed enumeration",
            validate_schema_instance(schema, held),
        )

    def test_collection_dispute_release_migration_allows_released_status(self) -> None:
        """The release migration adds released status without a second hold row."""
        sql = (ROOT / "database/migrations/0032_collection_dispute_release.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "collection_dispute_status IN ('held', 'released')",
            "ADD COLUMN released_at timestamptz",
            "collection_dispute_status = 'released'",
        ):
            self.assertIn(expected_fragment, sql)

    def test_void_journal_migration_reuses_journal_proposal_for_voids(self) -> None:
        """Void proposals reuse journal_proposal and add a void-scoped identity."""
        sql = (ROOT / "database/migrations/0030_void_journal_proposal.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN issued_invoice_void_id uuid",
            "FOREIGN KEY (tenant_account_id, issued_invoice_void_id)",
            "REFERENCES billing_core.issued_invoice_void (tenant_account_id, issued_invoice_void_id)",
            "CREATE UNIQUE INDEX journal_proposal_issued_invoice_void_identity",
            "issued_invoice_void_id IS NOT NULL",
        ):
            self.assertIn(expected_fragment, sql)

    def test_credit_note_void_journal_migration_reuses_journal_proposal(self) -> None:
        """Credit-note void proposals reuse journal_proposal and add a void identity."""
        sql = (
            ROOT / "database/migrations/0034_credit_note_void_journal_proposal.sql"
        ).read_text(encoding="utf-8")
        for expected_fragment in (
            "ADD COLUMN issued_credit_note_void_id uuid",
            "FOREIGN KEY (tenant_account_id, issued_credit_note_void_id)",
            "REFERENCES billing_core.issued_credit_note_void (tenant_account_id, issued_credit_note_void_id)",
            "CREATE UNIQUE INDEX journal_proposal_issued_credit_note_void_identity",
            "issued_credit_note_void_id IS NOT NULL",
        ):
            self.assertIn(expected_fragment, sql)

    def test_write_off_journal_migration_reuses_journal_proposal_for_write_offs(self) -> None:
        """Write-off proposals reuse journal_proposal and add a write-off-scoped identity."""
        sql = (ROOT / "database/migrations/0022_write_off_journal_proposal.sql").read_text(
            encoding="utf-8"
        )
        for expected_fragment in (
            "ADD COLUMN collection_write_off_id uuid",
            "FOREIGN KEY (tenant_account_id, collection_write_off_id)",
            "REFERENCES billing_core.collection_write_off (tenant_account_id, collection_write_off_id)",
            "CREATE UNIQUE INDEX journal_proposal_write_off_identity",
            "collection_write_off_id IS NOT NULL",
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
            runtime_requirements = copied_root / "requirements-runtime.txt"
            runtime_requirements.write_text("psycopg==3.3.4\n", encoding="utf-8")
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
        self.assertIn("runtime dependencies must be hash locked", errors)
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

    def test_applied_legacy_dimensions_migration_is_allowlisted_by_path(self) -> None:
        """The immutable 0040 migration remains valid while new SQL stays strict."""
        self.assertEqual(validate_repository(ROOT), ())
        self.assertEqual(
            validate_sql_object_names(
                "ALTER TABLE usage_event ADD COLUMN dimensions jsonb;\n"
            ),
            ("column name must contain at least two snake_case words: dimensions",),
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
