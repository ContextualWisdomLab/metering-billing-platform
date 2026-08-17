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
        ):
            self.assertIn(story_name, inventory)
        for fixture_name in (
            FIXTURE_NAMES
            + COLLECTION_FIXTURE_NAMES
            + PAYMENT_INTENT_FIXTURE_NAMES
            + PAYMENT_RECEIPT_FIXTURE_NAMES
        ):
            self.assertIn(fixture_name, inventory)
        self.assertIn("Collect or credit", inventory)
        self.assertIn("Open the collection case, then collect or credit", inventory)
        self.assertIn("Create a projected payment intent, then record the receipt", inventory)
        self.assertIn(
            "Record the receipt; the cash journal is already validated for AIS to pull",
            inventory,
        )

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
