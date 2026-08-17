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
