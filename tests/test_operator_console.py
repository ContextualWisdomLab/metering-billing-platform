"""Operator presentment console tests for Storybook fixtures and exact decimals."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from metering_billing.contracts import (
    validate_collection_case_presentment,
    validate_invoice_presentment,
    validate_payment_intent_presentment,
    validate_payment_receipt_presentment,
    validate_credit_adjustment_presentment,
    validate_rate_card_presentment,
    validate_usage_event_presentment,
    validate_rating_run_presentment,
    validate_tax_assessment_presentment,
    validate_posting_receipt_observation_presentment,
    validate_webhook_delivery_presentment,
    validate_tenant_api_credential_presentment,
    validate_webhook_subscription_presentment,
    validate_dunning_event_presentment,
    validate_webhook_outbox_event_presentment,
    validate_issued_credit_note_presentment,
    validate_credit_note_application_presentment,
    validate_collection_case_settlement_presentment,
    validate_account_statement_presentment,
    validate_spend_budget_evaluation_presentment,
    validate_rated_spend_presentment,
    validate_billing_account_budget_status_presentment,
)
from metering_billing.exact_decimal import parse_exact_decimal


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_ROOT = ROOT / "operator_console"
FIXTURE_NAMES = (
    "taxed_partial_credit.json",
    "untaxed_morning.json",
    "settled_statement.json",
)
COLLECTION_FIXTURE_NAMES = (
    "open_collection_case.json",
    "dunning_collection_case.json",
    "settled_collection_case.json",
)
PAYMENT_INTENT_FIXTURE_NAMES = (
    "projected_payment_intent.json",
    "cancelled_payment_intent.json",
)
PAYMENT_RECEIPT_FIXTURE_NAMES = (
    "applied_full_payment_receipt.json",
    "applied_partial_payment_receipt.json",
)
CREDIT_ADJUSTMENT_FIXTURE_NAMES = (
    "recorded_morning_credit.json",
    "recorded_taxed_credit.json",
)
RATE_CARD_FIXTURE_NAMES = (
    "published_standard_rate.json",
    "published_premium_rate.json",
)
USAGE_EVENT_FIXTURE_NAMES = (
    "stored_morning_usage.json",
    "stored_partial_token_usage.json",
)
RATING_RUN_FIXTURE_NAMES = (
    "rated_morning_window.json",
    "rated_partial_window.json",
)
TAX_ASSESSMENT_FIXTURE_NAMES = (
    "assessed_morning_vat.json",
    "assessed_partial_vat.json",
)
POSTING_RECEIPT_OBSERVATION_FIXTURE_NAMES = (
    "observed_posted_morning.json",
    "observed_held_receipt.json",
)
WEBHOOK_DELIVERY_FIXTURE_NAMES = (
    "delivered_morning.json",
    "failed_callback.json",
)
TENANT_API_CREDENTIAL_FIXTURE_NAMES = (
    "active_operator_key.json",
    "revoked_leaked_key.json",
)
WEBHOOK_SUBSCRIPTION_FIXTURE_NAMES = (
    "active_https_callback.json",
    "revoked_https_callback.json",
)
DUNNING_NOTICE_FIXTURE_NAMES = (
    "first_notice_morning.json",
    "overdue_notice_evening.json",
)
WEBHOOK_OUTBOX_EVENT_FIXTURE_NAMES = (
    "pending_journal_validated.json",
    "delivered_receipt_applied.json",
)
ISSUED_CREDIT_NOTE_FIXTURE_NAMES = (
    "issued_morning_credit_note.json",
    "issued_taxed_credit_note.json",
)
CREDIT_NOTE_APPLICATION_FIXTURE_NAMES = ("applied_morning_credit_note.json",)
COLLECTION_CASE_SETTLEMENT_FIXTURE_NAMES = ("settled_morning_zero.json",)
ACCOUNT_STATEMENT_FIXTURE_NAMES = (
    "settled_account_statement.json",
    "voided_account_statement.json",
)
SPEND_BUDGET_FIXTURE_NAMES = (
    "published_under_budget.json",
    "published_at_budget.json",
    "published_over_budget.json",
)
RATED_SPEND_FIXTURE_NAMES = (
    "rated_spend_morning_product.json",
    "rated_spend_morning_project.json",
)
BUDGET_STATUS_FIXTURE_NAMES = (
    "account_budget_status_under_over.json",
    "account_budget_status_next_cursor.json",
)
SPEND_BUDGET_MONEY_FIELDS = (
    "budget_amount",
    "rated_amount",
    "remaining_amount",
    "over_amount",
)
ACCOUNT_STATEMENT_MONEY_FIELDS = (
    "issued_invoice_total",
    "voided_invoice_total",
    "open_collection_remaining",
    "applied_credit_total",
    "voided_credit_total",
    "write_off_total",
    "parked_unapplied_cash",
    "refunded_unapplied_cash",
)
MONEY_FIELDS = (
    "tax_exclusive_amount",
    "tax_amount",
    "tax_inclusive_amount",
    "credited_amount",
    "amount_due",
)
LINE_MONEY_FIELDS = ("quantity", "unit_amount", "line_amount")


class OperatorConsoleTests(unittest.TestCase):
    """Prove Storybook fixtures render the #21 statement as exact-decimal strings."""

    def test_fixtures_match_presentment_contract_without_floats(self) -> None:
        """Taxed, untaxed, and settled fixtures must be schema-valid string money."""
        for fixture_name in FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_invoice_presentment(payload), ())
            for field_name in MONEY_FIELDS:
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
            for line in payload["invoice_lines"]:
                for field_name in LINE_MONEY_FIELDS:
                    value = line[field_name]
                    self.assertIsInstance(value, str)
                    self.assertNotIsInstance(value, float)
                    parse_exact_decimal(value)
        taxed = self._fixture("taxed_partial_credit.json")
        self.assertEqual(taxed["amount_due"], "99.00")
        self.assertEqual(taxed["tax_inclusive_amount"], "110.00")
        self.assertEqual(taxed["credited_amount"], "11.00")
        untaxed = self._fixture("untaxed_morning.json")
        self.assertEqual(untaxed["tax_amount"], "0")
        self.assertEqual(untaxed["amount_due"], "0.003705")
        settled = self._fixture("settled_statement.json")
        self.assertEqual(settled["amount_due"], "0.00")
        self.assertEqual(settled["credited_amount"], settled["tax_inclusive_amount"])
        for fixture_name in COLLECTION_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_collection_case_presentment(payload), ())
            value = payload["collection_outstanding"]
            self.assertIsInstance(value, str)
            self.assertNotIsInstance(value, float)
            parse_exact_decimal(value)
        self.assertEqual(self._fixture("open_collection_case.json")["next_operator_action"], "collect")
        self.assertEqual(self._fixture("dunning_collection_case.json")["last_dunning_notice_code"], "first_notice")
        self.assertEqual(self._fixture("settled_collection_case.json")["next_operator_action"], "wait")
        for fixture_name in PAYMENT_INTENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_payment_intent_presentment(payload), ())
            value = payload["payment_amount"]
            self.assertIsInstance(value, str)
            self.assertNotIsInstance(value, float)
            parse_exact_decimal(value)
        self.assertEqual(
            self._fixture("projected_payment_intent.json")["next_operator_action"],
            "record_receipt",
        )
        self.assertEqual(self._fixture("cancelled_payment_intent.json")["next_operator_action"], "wait")
        for fixture_name in PAYMENT_RECEIPT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_payment_receipt_presentment(payload), ())
            for field_name in ("received_amount", "remaining_outstanding_amount"):
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
        self.assertEqual(
            self._fixture("applied_full_payment_receipt.json")["next_operator_action"],
            "drain_or_wait",
        )
        self.assertEqual(
            self._fixture("applied_partial_payment_receipt.json")["next_operator_action"],
            "record_receipt",
        )
        for fixture_name in CREDIT_ADJUSTMENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_credit_adjustment_presentment(payload), ())
            for field_name in ("credit_amount", "tax_exclusive_amount", "tax_amount"):
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
        self.assertEqual(self._fixture("recorded_morning_credit.json")["next_operator_action"], "wait")
        self.assertEqual(self._fixture("recorded_taxed_credit.json")["credit_amount"], "11.00")
        for fixture_name in RATE_CARD_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_rate_card_presentment(payload), ())
            for line in payload["lines"]:
                value = line["unit_amount"]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
        self.assertEqual(
            self._fixture("published_standard_rate.json")["next_operator_action"],
            "rate_window",
        )
        self.assertEqual(
            self._fixture("published_premium_rate.json")["lines"][0]["unit_amount"],
            "0.000005",
        )
        for fixture_name in USAGE_EVENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_usage_event_presentment(payload), ())
            for measurement in payload["measurements"]:
                value = measurement["quantity"]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
        self.assertEqual(
            self._fixture("stored_morning_usage.json")["next_operator_action"],
            "rate_window",
        )
        self.assertEqual(
            self._fixture("stored_partial_token_usage.json")["measurements"][0]["quantity"],
            "42.5",
        )
        for fixture_name in RATING_RUN_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_rating_run_presentment(payload), ())
            value = payload["rated_total_amount"]
            self.assertIsInstance(value, str)
            self.assertNotIsInstance(value, float)
            parse_exact_decimal(value)
            for line in payload["rating_lines"]:
                for field_name in ("rated_quantity", "unit_price_amount", "line_total_amount"):
                    line_value = line[field_name]
                    self.assertIsInstance(line_value, str)
                    self.assertNotIsInstance(line_value, float)
                    parse_exact_decimal(line_value)
        self.assertEqual(
            self._fixture("rated_morning_window.json")["next_operator_action"],
            "draft_invoice",
        )
        self.assertEqual(
            self._fixture("rated_partial_window.json")["rated_total_amount"],
            "0.000085",
        )
        for fixture_name in TAX_ASSESSMENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_tax_assessment_presentment(payload), ())
            for field_name in ("tax_exclusive_amount", "tax_amount", "tax_inclusive_amount"):
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
        self.assertEqual(
            self._fixture("assessed_morning_vat.json")["next_operator_action"],
            "propose_journal",
        )
        self.assertEqual(
            self._fixture("assessed_partial_vat.json")["tax_inclusive_amount"],
            "22.00",
        )
        for fixture_name in POSTING_RECEIPT_OBSERVATION_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_posting_receipt_observation_presentment(payload), ())
            self.assertEqual(payload["next_operator_action"], "wait")
            self.assertNotIn("proposal_status", payload)
        self.assertEqual(
            self._fixture("observed_posted_morning.json")["posting_status_code"],
            "posted",
        )
        self.assertEqual(
            self._fixture("observed_held_receipt.json")["posting_status_code"],
            "held",
        )
        for fixture_name in WEBHOOK_DELIVERY_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_webhook_delivery_presentment(payload), ())
            self.assertNotIn("webhook_secret", payload)
            self.assertNotIn("webhook_secret_hash", payload)
            self.assertNotIn("payload_json", payload)
            self.assertNotIn("delivery_status", payload)
        self.assertEqual(self._fixture("delivered_morning.json")["next_operator_action"], "wait")
        self.assertEqual(
            self._fixture("failed_callback.json")["next_operator_action"],
            "run_deliveries",
        )
        for fixture_name in TENANT_API_CREDENTIAL_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_tenant_api_credential_presentment(payload), ())
            self.assertNotIn("api_credential_secret", payload)
            self.assertNotIn("credential_secret_hash", payload)
            self.assertTrue(str(payload["credential_prefix"]).startswith("cwlak_fake"))
        self.assertEqual(self._fixture("active_operator_key.json")["next_operator_action"], "wait")
        self.assertEqual(self._fixture("revoked_leaked_key.json")["next_operator_action"], "issue")
        for fixture_name in WEBHOOK_SUBSCRIPTION_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_webhook_subscription_presentment(payload), ())
            self.assertNotIn("webhook_secret", payload)
            self.assertNotIn("webhook_secret_hash", payload)
            self.assertNotIn("webhook_secret_prefix", payload)
            self.assertNotIn("payload_json", payload)
            self.assertTrue(str(payload["callback_url"]).startswith("https://hooks.example.test/"))
        self.assertEqual(
            self._fixture("active_https_callback.json")["next_operator_action"],
            "run_deliveries",
        )
        self.assertEqual(
            self._fixture("revoked_https_callback.json")["next_operator_action"],
            "register",
        )
        for fixture_name in DUNNING_NOTICE_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_dunning_event_presentment(payload), ())
            self.assertNotIn("recipient", payload)
            self.assertNotIn("delivery_status", payload)
            self.assertNotIn("body", payload)
            self.assertIn(payload["dunning_notice_code"], {"first_notice", "overdue_notice"})
        self.assertEqual(
            self._fixture("first_notice_morning.json")["next_operator_action"],
            "collect",
        )
        self.assertEqual(
            self._fixture("overdue_notice_evening.json")["dunning_notice_code"],
            "overdue_notice",
        )
        for fixture_name in WEBHOOK_OUTBOX_EVENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_webhook_outbox_event_presentment(payload), ())
            self.assertNotIn("payload_json", payload)
            self.assertNotIn("webhook_secret", payload)
            self.assertNotIn("webhook_secret_hash", payload)
            self.assertNotIn("signature", payload)
            self.assertTrue(str(payload["payload_hash"]).startswith("sha256:"))
        self.assertEqual(
            self._fixture("pending_journal_validated.json")["next_operator_action"],
            "run_deliveries",
        )
        self.assertEqual(
            self._fixture("delivered_receipt_applied.json")["delivery_status"],
            "delivered",
        )
        for fixture_name in ISSUED_CREDIT_NOTE_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_issued_credit_note_presentment(payload), ())
            for field_name in ("tax_exclusive_amount", "tax_amount", "tax_inclusive_amount"):
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
            self.assertEqual(payload["issued_credit_note_status"], "issued")
            self.assertNotIn("credit_note_number", payload)
            self.assertNotIn("legal_credit_note_number", payload)
            self.assertNotIn("issued_credit_note_lines", payload)
        self.assertEqual(
            self._fixture("issued_morning_credit_note.json")["next_operator_action"],
            "wait",
        )
        self.assertEqual(self._fixture("issued_taxed_credit_note.json")["tax_inclusive_amount"], "11.00")
        for fixture_name in CREDIT_NOTE_APPLICATION_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_credit_note_application_presentment(payload), ())
            for field_name in ("applied_amount", "remaining_outstanding_amount"):
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
            self.assertEqual(payload["credit_note_application_status"], "applied")
            self.assertNotIn("legal_credit_note_number", payload)
            self.assertNotIn("credit_note_application_outcome_code", payload)
        self.assertEqual(
            self._fixture("applied_morning_credit_note.json")["next_operator_action"],
            "wait",
        )
        for fixture_name in COLLECTION_CASE_SETTLEMENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_collection_case_settlement_presentment(payload), ())
            remaining = payload["remaining_outstanding_amount"]
            self.assertIsInstance(remaining, str)
            self.assertNotIsInstance(remaining, float)
            parse_exact_decimal(remaining)
            self.assertEqual(payload["collection_case_settlement_status"], "settled")
            self.assertNotIn("write_off_amount", payload)
            self.assertNotIn("collection_case_settlement_outcome_code", payload)
        self.assertEqual(
            self._fixture("settled_morning_zero.json")["next_operator_action"],
            "wait",
        )
        for fixture_name in ACCOUNT_STATEMENT_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_account_statement_presentment(payload), ())
            self.assertEqual(len(payload["currencies"]), 1)
            row = payload["currencies"][0]
            self.assertEqual(row["currency_code"], "USD")
            for field_name in ACCOUNT_STATEMENT_MONEY_FIELDS:
                value = row[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
            self.assertNotIn("card_pan", payload)
            self.assertNotIn("statutory_account_id", payload)
            self.assertNotIn("proposal_status", payload)
        settled_account = self._fixture("settled_account_statement.json")
        self.assertEqual(settled_account["currencies"][0]["open_collection_remaining"], "0.00")
        self.assertEqual(settled_account["currencies"][0]["applied_credit_total"], "110.00")
        self.assertEqual(settled_account["currencies"][0]["voided_invoice_total"], "0")
        self.assertEqual(settled_account["currencies"][0]["voided_credit_total"], "0")
        voided_account = self._fixture("voided_account_statement.json")
        self.assertEqual(voided_account["currencies"][0]["issued_invoice_total"], "110.00")
        self.assertEqual(voided_account["currencies"][0]["voided_invoice_total"], "110.00")
        self.assertEqual(voided_account["currencies"][0]["voided_credit_total"], "11.00")
        self.assertEqual(voided_account["currencies"][0]["applied_credit_total"], "0")
        for fixture_name in SPEND_BUDGET_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_spend_budget_evaluation_presentment(payload), ())
            for field_name in SPEND_BUDGET_MONEY_FIELDS:
                value = payload[field_name]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
            self.assertEqual(payload["spend_budget_status"], "published")
            self.assertEqual(payload["next_operator_action"], "wait")
            self.assertIn(payload["utilization_status"], {"under", "at", "over"})
            self.assertEqual(payload["tenant_reference"], "urn:cwl:tenant_001")
            self.assertNotIn("card_pan", payload)
            self.assertNotIn("retained_earnings", payload)
            self.assertNotIn("statutory_account_id", payload)
            self.assertNotIn("journal_entry_id", payload)
        under_budget = self._fixture("published_under_budget.json")
        self.assertEqual(under_budget["budget_amount"], "100.00")
        self.assertEqual(under_budget["rated_amount"], "0.003705")
        self.assertEqual(under_budget["remaining_amount"], "99.996295")
        self.assertEqual(under_budget["over_amount"], "0")
        self.assertEqual(under_budget["utilization_status"], "under")
        at_budget = self._fixture("published_at_budget.json")
        self.assertEqual(at_budget["budget_amount"], "0.003705")
        self.assertEqual(at_budget["remaining_amount"], "0")
        self.assertEqual(at_budget["over_amount"], "0")
        self.assertEqual(at_budget["utilization_status"], "at")
        over_budget = self._fixture("published_over_budget.json")
        self.assertEqual(over_budget["budget_amount"], "0.001")
        self.assertEqual(over_budget["over_amount"], "0.002705")
        self.assertEqual(over_budget["remaining_amount"], "0")
        self.assertEqual(over_budget["utilization_status"], "over")
        for fixture_name in RATED_SPEND_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_rated_spend_presentment(payload), ())
            self.assertEqual(payload["tenant_reference"], "urn:cwl:tenant_001")
            self.assertEqual(payload["window_started_at"], "2026-08-16T10:00:00Z")
            self.assertEqual(payload["window_ended_at"], "2026-08-16T11:00:00Z")
            self.assertNotIn("group_by", payload)
            self.assertNotIn("next_operator_action", payload)
            self.assertNotIn("card_pan", payload)
            self.assertNotIn("retained_earnings", payload)
            self.assertNotIn("statutory_account_id", payload)
            self.assertNotIn("journal_entry_id", payload)
            currencies = {row["currency_code"] for row in payload["products"]}
            self.assertEqual(currencies, {"USD"})
            for row in payload["products"]:
                value = row["rated_amount"]
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, float)
                parse_exact_decimal(value)
                self.assertNotIn("credential_reference", row)
                self.assertNotIn("billing_principal_reference", row)
                self.assertNotIn("cost_center_reference", row)
        product_spend = self._fixture("rated_spend_morning_product.json")
        self.assertEqual(len(product_spend["products"]), 1)
        self.assertEqual(product_spend["products"][0]["product_code"], "contextual_orchestrator")
        self.assertEqual(product_spend["products"][0]["rated_amount"], "0.003705")
        self.assertNotIn("project_reference", product_spend["products"][0])
        project_spend = self._fixture("rated_spend_morning_project.json")
        self.assertEqual(len(project_spend["products"]), 1)
        self.assertEqual(project_spend["products"][0]["product_code"], "contextual_orchestrator")
        self.assertEqual(project_spend["products"][0]["project_reference"], "urn:cwl:tenant_001:project:metering")
        self.assertEqual(project_spend["products"][0]["rated_amount"], "0.003705")
        for fixture_name in BUDGET_STATUS_FIXTURE_NAMES:
            payload = self._fixture(fixture_name)
            self.assertEqual(validate_billing_account_budget_status_presentment(payload), ())
            self.assertEqual(set(payload), {"budget_statuses", "next_cursor"})
            self.assertNotIn("tenant_reference", payload)
            self.assertNotIn("items", payload)
            self.assertNotIn("cursor", payload)
            self.assertNotIn("rated_amount", payload)
            self.assertNotIn("card_pan", payload)
            self.assertNotIn("retained_earnings", payload)
            self.assertNotIn("statutory_account_id", payload)
            self.assertNotIn("journal_entry_id", payload)
            currencies = {row["currency_code"] for row in payload["budget_statuses"]}
            self.assertEqual(currencies, {"USD"})
            for row in payload["budget_statuses"]:
                for field_name in SPEND_BUDGET_MONEY_FIELDS:
                    value = row[field_name]
                    self.assertIsInstance(value, str)
                    self.assertNotIsInstance(value, float)
                    parse_exact_decimal(value)
                self.assertEqual(row["spend_budget_status"], "published")
                self.assertEqual(row["next_operator_action"], "wait")
                self.assertIn(row["utilization_status"], {"under", "at", "over"})
        under_over = self._fixture("account_budget_status_under_over.json")
        self.assertEqual(under_over["next_cursor"], None)
        self.assertEqual(
            [row["utilization_status"] for row in under_over["budget_statuses"]],
            ["under", "at", "over"],
        )
        self.assertEqual(under_over["budget_statuses"][0]["remaining_amount"], "99.996295")
        self.assertEqual(under_over["budget_statuses"][2]["over_amount"], "0.002705")
        next_page = self._fixture("account_budget_status_next_cursor.json")
        self.assertEqual(
            next_page["next_cursor"],
            "2026-08-18T15:00:00Z|019d7b92-1aa0-7a7f-b61c-962c0f4bfe02",
        )
        self.assertEqual(len(next_page["budget_statuses"]), 2)
        self.assertEqual(next_page["budget_statuses"][0]["utilization_status"], "under")
        self.assertEqual(next_page["budget_statuses"][1]["utilization_status"], "at")

    def test_design_tokens_cover_color_spacing_type_and_radius(self) -> None:
        """Repeated modules must share tokenized color, spacing, type, and radius."""
        tokens = json.loads(
            (CONSOLE_ROOT / "tokens" / "design_tokens.json").read_text(encoding="utf-8")
        )
        for group_name in ("color", "spacing", "type", "radius"):
            self.assertIn(group_name, tokens)
            self.assertTrue(tokens[group_name])
        css_text = (CONSOLE_ROOT / "tokens" / "design_tokens.css").read_text(encoding="utf-8")
        self.assertIn("--oc-color-", css_text)
        self.assertIn("--oc-spacing-", css_text)
        self.assertIn("--oc-type-", css_text)
        self.assertIn("--oc-radius-", css_text)

    def test_storybook_inventory_lists_required_stories(self) -> None:
        """The inventory must name the four tokenized modules and three fixtures."""
        inventory = (ROOT / "docs" / "STORYBOOK.md").read_text(encoding="utf-8")
        for story_name in (
            "InvoiceStatement",
            "AmountDue",
            "LineTable",
            "StatusChip",
            "CollectionCase",
            "PaymentIntent",
            "PaymentReceipt",
            "CreditAdjustment",
            "RateCard",
            "UsageEvent",
            "RatingRun",
            "TaxAssessment",
            "PostingReceiptObservation",
            "WebhookDelivery",
            "TenantApiCredential",
            "WebhookSubscription",
            "DunningNotice",
            "WebhookOutboxEvent",
            "IssuedInvoice",
            "IssuedCreditNote",
            "CreditNoteApplication",
            "CollectionCaseSettlement",
            "AccountStatement",
            "SpendBudget",
            "RatedSpend",
            "BudgetStatus",
        ):
            self.assertIn(story_name, inventory)
        for fixture_name in (
            FIXTURE_NAMES
            + COLLECTION_FIXTURE_NAMES
            + PAYMENT_INTENT_FIXTURE_NAMES
            + PAYMENT_RECEIPT_FIXTURE_NAMES
            + CREDIT_ADJUSTMENT_FIXTURE_NAMES
            + RATE_CARD_FIXTURE_NAMES
            + USAGE_EVENT_FIXTURE_NAMES
            + RATING_RUN_FIXTURE_NAMES
            + TAX_ASSESSMENT_FIXTURE_NAMES
            + POSTING_RECEIPT_OBSERVATION_FIXTURE_NAMES
            + WEBHOOK_DELIVERY_FIXTURE_NAMES
            + TENANT_API_CREDENTIAL_FIXTURE_NAMES
            + WEBHOOK_SUBSCRIPTION_FIXTURE_NAMES
            + DUNNING_NOTICE_FIXTURE_NAMES
            + WEBHOOK_OUTBOX_EVENT_FIXTURE_NAMES
            + ISSUED_CREDIT_NOTE_FIXTURE_NAMES
            + CREDIT_NOTE_APPLICATION_FIXTURE_NAMES
            + COLLECTION_CASE_SETTLEMENT_FIXTURE_NAMES
            + ACCOUNT_STATEMENT_FIXTURE_NAMES
            + SPEND_BUDGET_FIXTURE_NAMES
            + RATED_SPEND_FIXTURE_NAMES
            + BUDGET_STATUS_FIXTURE_NAMES
        ):
            self.assertIn(fixture_name, inventory)
        self.assertIn("Collect or credit", inventory)
        self.assertIn("Open the collection case, then collect or credit", inventory)
        self.assertIn("Create a projected payment intent, then record the receipt", inventory)
        self.assertIn(
            "Record the receipt; the cash journal is already validated for AIS to pull",
            inventory,
        )
        self.assertIn("Record the credit; AIS pulls the validated journal", inventory)
        self.assertIn("Publish a rate card, then rate a window against that version", inventory)
        self.assertIn("Ingest usage, then rate a window against a published card", inventory)
        self.assertIn("Rate a window, then draft an invoice", inventory)
        self.assertIn(
            "Publish a tax rate, assess the draft, then propose the journal and let AIS pull",
            inventory,
        )
        self.assertIn(
            "Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty",
            inventory,
        )
        self.assertIn(
            "Register an https callback, then run deliveries; AIS may keep polling",
            inventory,
        )
        self.assertIn("Issue a key, then send it on every /v1 call; revoke when leaked", inventory)
        self.assertIn("Record the commercial reminder, then collect or credit", inventory)
        self.assertIn("Issue invoice, then collect or credit", inventory)
        self.assertIn(
            "Issue the credit note; the validated journal remains available for AIS",
            inventory,
        )
        self.assertIn(
            "Apply the issued credit note, then collect the residual",
            inventory,
        )
        self.assertIn("Settle the zero-outstanding case, then wait", inventory)
        self.assertIn(
            "Open the account statement, then collect, credit, park, apply, or refund",
            inventory,
        )
        self.assertIn("Publish a commercial spend budget, then wait", inventory)
        self.assertIn(
            "Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice",
            inventory,
        )
        self.assertIn("Open the account budget status, then wait", inventory)

    def test_node_renderer_prints_exact_decimal_strings(self) -> None:
        """Vanilla modules must emit fixture amounts as strings, never floats."""
        completed = subprocess.run(
            ["node", "--test", "tests/render_fixtures.test.js"],
            cwd=CONSOLE_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )

    def test_console_lint_rejects_float_money(self) -> None:
        """The console linter must fail closed on IEEE binary money."""
        completed = subprocess.run(
            ["node", "scripts/lint_console.mjs"],
            cwd=CONSOLE_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )

    def _fixture(self, fixture_name: str) -> dict[str, object]:
        """Load one checked-in presentment fixture."""
        path = CONSOLE_ROOT / "fixtures" / fixture_name
        return json.loads(path.read_text(encoding="utf-8"))
