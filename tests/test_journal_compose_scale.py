"""Journal compose fail-close when AIS cannot accept more than six fractional digits."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal

from metering_billing import (
    AccountingExportService,
    CreditAdjustmentService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.accounting_export import parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    ExactDecimalError,
    JournalLineAmountScaleError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.exact_decimal import (
    journal_line_amount_exceeds_postable_scale,
    require_postable_journal_line_amounts,
)
from metering_billing.usage_ledger import generate_record_id
from scripts.validate_repository import validate_accounting_journal_proposal
from test_credit_journal_proposal import record_morning_credit_without_journal
from test_http_app import invoke_http
from test_journal_proposal import draft_known_morning
from test_repository_contracts import ROOT
from test_usage_ingestion import TENANT_ONE
from test_usage_rating import KNOWN_MORNING_TOTAL


SEVEN_PLACE_AMOUNT = Decimal("0.0000001")
SIGNIFICANT_SEVEN_PLACE_AMOUNT = Decimal("0.0037051")
SIX_PLACE_AMOUNT = Decimal("0.003705")
SIX_TRAILING_ZEROS = Decimal("1.000000")
SEVEN_TRAILING_ZEROS = Decimal("1.0000000")
INTEGER_AMOUNT = Decimal("10")


def _draft_with_total(amount: Decimal):
    """Persist the known morning draft, then replace only its commercial total."""
    ledger, invoice_draft_id = draft_known_morning()
    stored = ledger.get_invoice_draft(invoice_draft_id)
    if stored is None:
        raise AssertionError("known morning path must persist an invoice draft")
    ledger.invoice_drafts[invoice_draft_id] = replace(stored, drafted_total_amount=amount)
    return ledger, invoice_draft_id


class JournalComposeScaleTests(unittest.TestCase):
    """Reject unpostable journal-line scale before AIS can pull a proposal."""

    def test_seven_fractional_digit_debit_fails_closed_before_compose(self) -> None:
        """A debit with seven places must not persist or become an AIS pull document."""
        ledger, invoice_draft_id = _draft_with_total(SEVEN_PLACE_AMOUNT)
        rejected = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(rejected.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.JOURNAL_LINE_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertNotIn("proposal_id", rejected.as_contract_dict())
        self.assertNotIn("lines", rejected.as_contract_dict())

    def test_seven_fractional_digit_credit_line_fails_closed(self) -> None:
        """Credit-side scale is the same AIS boundary as debit-side scale."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal(
            SEVEN_PLACE_AMOUNT
        )
        rejected = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        self.assertEqual(rejected.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.JOURNAL_LINE_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_integer_and_six_place_amounts_still_compose(self) -> None:
        """Integers and exactly six fractional digits remain postable Exact Decimals."""
        ledger, six_draft_id = _draft_with_total(SIX_PLACE_AMOUNT)
        six = AccountingExportService(ledger).propose_journal(TENANT_ONE, six_draft_id)
        self.assertEqual(six.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(six.proposal_lines[0].debit_amount, SIX_PLACE_AMOUNT)
        self.assertEqual(six.proposal_lines[1].credit_amount, SIX_PLACE_AMOUNT)
        self.assertEqual(validate_journal_proposal(six.as_contract_dict()), ())

        integer_ledger, integer_draft_id = _draft_with_total(INTEGER_AMOUNT)
        integer = AccountingExportService(integer_ledger).propose_journal(
            TENANT_ONE, integer_draft_id
        )
        self.assertEqual(integer.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(integer.proposal_lines[0].debit_amount, INTEGER_AMOUNT)
        self.assertEqual(validate_journal_proposal(integer.as_contract_dict()), ())

        trailing_ledger, trailing_draft_id = _draft_with_total(SIX_TRAILING_ZEROS)
        trailing = AccountingExportService(trailing_ledger).propose_journal(
            TENANT_ONE, trailing_draft_id
        )
        self.assertEqual(trailing.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(trailing.proposal_lines[0].debit_amount, SIX_TRAILING_ZEROS)

        padded_ledger, padded_draft_id = _draft_with_total(SEVEN_TRAILING_ZEROS)
        padded = AccountingExportService(padded_ledger).propose_journal(
            TENANT_ONE, padded_draft_id
        )
        self.assertEqual(padded.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(padded.proposal_lines[0].debit_amount, SEVEN_TRAILING_ZEROS)
        self.assertEqual(format_exact_decimal(SEVEN_TRAILING_ZEROS), "1.0000000")

    def test_significant_seventh_digit_fails_closed_without_rounding(self) -> None:
        """A non-zero seventh place is unpostable and must not be quantized into six places."""
        ledger, invoice_draft_id = _draft_with_total(SIGNIFICANT_SEVEN_PLACE_AMOUNT)
        rejected = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.JOURNAL_LINE_AMOUNT_INVALID,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)
        self.assertEqual(format_exact_decimal(SIGNIFICANT_SEVEN_PLACE_AMOUNT), "0.0037051")

    def test_known_morning_total_stays_six_places_and_exact(self) -> None:
        """Token quantity times unit price stays exact and still composes."""
        self.assertEqual(KNOWN_MORNING_TOTAL, SIX_PLACE_AMOUNT)
        ledger, invoice_draft_id = draft_known_morning()
        accepted = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(accepted.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(accepted.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(accepted.proposal_lines[0].debit_amount, SIX_PLACE_AMOUNT)

    def test_float_money_still_fails_closed_and_scale_is_not_rounded(self) -> None:
        """IEEE money stays ExactDecimalError; seven-place text is not quantized to 6."""
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.0000001)
        self.assertEqual(parse_proposal_amount("0.003705"), SIX_PLACE_AMOUNT)
        self.assertEqual(parse_proposal_amount(SEVEN_PLACE_AMOUNT), SEVEN_PLACE_AMOUNT)
        self.assertEqual(format_exact_decimal(SEVEN_PLACE_AMOUNT), "0.0000001")

    def test_insert_rejects_seven_place_debit_and_credit_without_rounding(self) -> None:
        """Ledger insert cannot persist an unpostable line even if compose is bypassed."""
        ledger, invoice_draft_id = draft_known_morning()
        first = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        stored = ledger.journal_proposals[first.proposal_id]
        debit_excess = (
            replace(stored.proposal_lines[0], debit_amount=SEVEN_PLACE_AMOUNT),
            replace(stored.proposal_lines[1], credit_amount=SEVEN_PLACE_AMOUNT),
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(
                replace(
                    stored,
                    journal_proposal_id=generate_record_id(),
                    invoice_draft_id=generate_record_id(),
                    source_payload_hash="sha256:" + "c" * 64,
                    proposal_lines=debit_excess,
                ),
                debit_excess,
            )
        credit_excess = (
            replace(stored.proposal_lines[0], debit_amount=INTEGER_AMOUNT),
            replace(stored.proposal_lines[1], credit_amount=Decimal("10.0000001")),
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(
                replace(
                    stored,
                    journal_proposal_id=generate_record_id(),
                    invoice_draft_id=generate_record_id(),
                    source_payload_hash="sha256:" + "d" * 64,
                    proposal_lines=credit_excess,
                ),
                credit_excess,
            )
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_credit_accept_fails_closed_before_composing_an_unpostable_journal(self) -> None:
        """Credit accept already composes; unpostable scale must not write the credit."""
        ledger, invoice_draft_id = _draft_with_total(SEVEN_PLACE_AMOUNT)
        rejected = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, SEVEN_PLACE_AMOUNT, "goodwill"
        )
        self.assertEqual(rejected.credit_adjustment_outcome_code.value, "rejected")
        self.assertEqual(len(ledger.credit_adjustments), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_http_compose_returns_422_and_does_not_emit_a_proposal(self) -> None:
        """POST /v1/journal-proposals stays compose-only and fail-closes at HTTP 422."""
        ledger, invoice_draft_id = _draft_with_total(SEVEN_PLACE_AMOUNT)
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            "/v1/journal-proposals",
            {"tenant_reference": TENANT_ONE, "invoice_draft_id": str(invoice_draft_id)},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["journal_proposal_outcome_code"], "rejected")
        self.assertEqual(body["rejection_reason_code"], "journal_line_amount_invalid")
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_published_schema_rejects_seven_place_line_amounts(self) -> None:
        """AIS pull documents cannot carry more than six fractional digits."""
        schema = json.loads(
            (ROOT / "schemas/accounting-journal-proposal.schema.json").read_text(encoding="utf-8")
        )
        proposal = {
            "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf61d",
            "proposal_contract_version": 1,
            "idempotency_key": "invoice_019d:scale:v1",
            "tenant_reference": "urn:cwl:tenant_001",
            "legal_entity_reference": "urn:cwl:entity_001",
            "intended_book_role_code": "primary_statutory",
            "transaction_currency": "USD",
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
                    "debit_amount": "0.0000001",
                    "credit_amount": "0",
                },
                {
                    "line_number": 2,
                    "account_role_code": "usage_revenue",
                    "debit_amount": "0",
                    "credit_amount": "0.0000001",
                },
            ],
        }
        errors = validate_accounting_journal_proposal(schema, proposal)
        self.assertTrue(
            any("six fractional digits" in error or "debit_amount" in error for error in errors)
        )
        six_place = dict(proposal)
        six_place["lines"] = [
            {
                "line_number": 1,
                "account_role_code": "accounts_receivable",
                "debit_amount": "0.003705",
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": "usage_revenue",
                "debit_amount": "0",
                "credit_amount": "0.003705",
            },
        ]
        self.assertEqual(validate_accounting_journal_proposal(schema, six_place), ())
        integer = dict(proposal)
        integer["lines"] = [
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
        ]
        self.assertEqual(validate_accounting_journal_proposal(schema, integer), ())

    def test_nan_and_infinite_amounts_exceed_postable_scale(self) -> None:
        """NaN and infinity cannot be represented at AIS numeric(38, 6)."""
        self.assertTrue(journal_line_amount_exceeds_postable_scale(Decimal("NaN")))
        self.assertTrue(journal_line_amount_exceeds_postable_scale(Decimal("Infinity")))
        self.assertTrue(journal_line_amount_exceeds_postable_scale(Decimal("-Infinity")))
        self.assertFalse(journal_line_amount_exceeds_postable_scale(SIX_PLACE_AMOUNT))
        self.assertFalse(journal_line_amount_exceeds_postable_scale(SEVEN_TRAILING_ZEROS))
        with self.assertRaises(JournalLineAmountScaleError):
            require_postable_journal_line_amounts(Decimal("NaN"))
        require_postable_journal_line_amounts(SIX_PLACE_AMOUNT, INTEGER_AMOUNT)


if __name__ == "__main__":
    unittest.main()
