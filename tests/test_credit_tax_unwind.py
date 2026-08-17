"""Tax-payable unwind tests for taxed credit adjustments."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    CreditAdjustmentService,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_credit_adjustment, validate_journal_proposal
from metering_billing.credit_adjustment import (
    CreditAdjustmentResult,
    CreditSplitError,
    split_inclusive_credit,
)
from metering_billing.errors import (
    CreditAdjustmentOutcomeCode,
    CreditAdjustmentRejectionReasonCode,
    ExactDecimalError,
)
from metering_billing.tax_assessment import round_tax_amount
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, HUNDRED_INT, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import seed_rated_ledger


class CreditTaxUnwindTests(unittest.TestCase):
    """Verify proportional tax split and three-line credit journals."""

    def test_full_taxed_credit_reconstructs_original_exclusive_and_tax(self) -> None:
        """A full inclusive credit must unwind the original exclusive and tax."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        usd_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        krw_id = insert_commercial_draft(ledger, TENANT_ONE, "KRW", HUNDRED_INT)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, usd_id, 1)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, krw_id, 1)
        usd = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, usd_id, Decimal("110.00"), "rating_correction"
        )
        krw = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, krw_id, Decimal("110"), "rating_correction"
        )
        self.assertEqual(usd.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.ACCEPTED)
        self.assertEqual(usd.tax_exclusive_amount, HUNDRED)
        self.assertEqual(usd.tax_amount, Decimal("10.00"))
        self.assertEqual(usd.tax_exclusive_amount + usd.tax_amount, usd.credit_amount)
        self.assertEqual(krw.tax_exclusive_amount, HUNDRED_INT)
        self.assertEqual(krw.tax_amount, Decimal("10"))
        usd_proposal = ledger.get_journal_proposal(usd.proposal_id)
        assert usd_proposal is not None
        self.assertEqual(usd_proposal.proposal_status, "validated")
        revenue, payable, receivable = usd_proposal.proposal_lines
        self.assertEqual(revenue.account_role_code, "usage_revenue")
        self.assertEqual(revenue.debit_amount, HUNDRED)
        self.assertEqual(payable.account_role_code, "tax_payable")
        self.assertEqual(payable.debit_amount, Decimal("10.00"))
        self.assertEqual(receivable.account_role_code, "accounts_receivable")
        self.assertEqual(receivable.credit_amount, Decimal("110.00"))
        self.assertEqual(
            revenue.debit_amount + payable.debit_amount, receivable.credit_amount
        )
        self.assertEqual(validate_credit_adjustment(usd.as_contract_dict()), ())
        exported = AccountingExportService(ledger).get_journal_proposal(
            TENANT_ONE, usd.proposal_id
        )
        self.assertEqual(validate_journal_proposal(exported.as_contract_dict()), ())
        self.assertEqual(exported.proposal_status, "validated")

    def test_partial_split_sums_and_collection_uses_inclusive(self) -> None:
        """A partial credit split sums to the inclusive amount and reduces outstanding."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
        result = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("11.00"), "goodwill"
        )
        self.assertEqual(result.tax_exclusive_amount, Decimal("10.00"))
        self.assertEqual(result.tax_amount, Decimal("1.00"))
        self.assertEqual(result.tax_exclusive_amount + result.tax_amount, result.credit_amount)
        self.assertEqual(result.remaining_adjustable_amount, Decimal("99.00"))
        self.assertEqual(result.remaining_outstanding_amount, Decimal("99.00"))
        replay = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("11.00"), "goodwill"
        )
        self.assertEqual(replay.credit_adjustment_outcome_code, CreditAdjustmentOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.credit_adjustment_id, result.credit_adjustment_id)
        self.assertEqual(replay.proposal_id, result.proposal_id)
        self.assertEqual(len(ledger.credit_adjustments), 1)

    def test_half_even_proportional_split_and_untaxed_stays_two_line(self) -> None:
        """0.055 * 10 / 110 is 0.005 and half-even USD tax is 0.00; untaxed stays two-line."""
        self.assertEqual(
            split_inclusive_credit(
                Decimal("0.055"), Decimal("10.00"), Decimal("110.00"), "USD"
            ),
            (Decimal("0.055"), Decimal("0.00")),
        )
        self.assertEqual(round_tax_amount(Decimal("0.005"), "USD"), Decimal("0.00"))
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        taxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, taxed_id, 1)
        half = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, taxed_id, Decimal("0.055"), "billing_error"
        )
        self.assertEqual(half.tax_amount, Decimal("0.00"))
        self.assertEqual(half.tax_exclusive_amount, Decimal("0.055"))
        half_proposal = ledger.get_journal_proposal(half.proposal_id)
        assert half_proposal is not None
        self.assertEqual(len(half_proposal.proposal_lines), 2)
        untaxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        untaxed = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, untaxed_id, Decimal("10.00"), "goodwill"
        )
        self.assertEqual(untaxed.tax_exclusive_amount, Decimal("10.00"))
        self.assertEqual(untaxed.tax_amount, Decimal("0"))
        untaxed_proposal = ledger.get_journal_proposal(untaxed.proposal_id)
        assert untaxed_proposal is not None
        self.assertEqual(len(untaxed_proposal.proposal_lines), 2)
        self.assertEqual(
            [line.account_role_code for line in untaxed_proposal.proposal_lines],
            ["usage_revenue", "accounts_receivable"],
        )
        self.assertNotEqual(half.source_payload_hash, untaxed.source_payload_hash)

    def test_http_journal_list_includes_three_line_unwind(self) -> None:
        """GET /v1/journal-proposals includes the validated tax-payable unwind."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft_id),
                "credit_amount": "110.00",
                "credit_reason_code": "rating_correction",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["tax_exclusive_amount"], "100.00")
        self.assertEqual(body["tax_amount"], "10.00")
        self.assertEqual(body["proposal_status"], "validated")
        self.assertEqual(validate_credit_adjustment(body), ())
        list_status, list_body = invoke_http(
            app, "GET", "/v1/journal-proposals", query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(list_status, 200)
        matched = next(
            item
            for item in list_body["journal_proposals"]
            if item["proposal_id"] == body["proposal_id"]
        )
        self.assertEqual(matched["proposal_status"], "validated")
        self.assertEqual(
            [line["account_role_code"] for line in matched["lines"]],
            ["usage_revenue", "tax_payable", "accounts_receivable"],
        )
        self.assertEqual(matched["lines"][1]["debit_amount"], "10.00")

    def test_fail_closed_split_and_corrupt_assessment(self) -> None:
        """Missing assessment fields, a non-summing split, and floats reject."""
        with self.assertRaises(CreditSplitError):
            split_inclusive_credit(Decimal("10.00"), Decimal("1.00"), Decimal("0"), "USD")
        with self.assertRaises(CreditSplitError):
            split_inclusive_credit(Decimal("10.00"), Decimal("20.00"), Decimal("10.00"), "USD")
        with self.assertRaises(CreditSplitError):
            split_inclusive_credit(Decimal("10.00"), Decimal("1.00"), Decimal("11.00"), "XXX")
        with self.assertRaises(ExactDecimalError):
            split_inclusive_credit(0.10, Decimal("1.00"), Decimal("11.00"), "USD")
        with self.assertRaises(CreditSplitError):
            split_inclusive_credit(Decimal("10.00"), "1e2", Decimal("11.00"), "USD")
        with mock.patch(
            "metering_billing.credit_adjustment.round_tax_amount",
            return_value=Decimal("99.00"),
        ):
            with self.assertRaises(CreditSplitError):
                split_inclusive_credit(
                    Decimal("10.00"), Decimal("1.00"), Decimal("11.00"), "USD"
                )
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        assessed = TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        stored = ledger.get_tax_assessment(assessed.tax_assessment_id)
        ledger.tax_assessments[stored.tax_assessment_id] = replace(
            stored, tax_amount=Decimal("99.00")
        )
        rejected = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("10.00"), "goodwill"
        )
        self.assertEqual(
            rejected.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.TAX_SPLIT_INVALID,
        )
        other = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_TWO, draft_id, Decimal("10.00"), "goodwill"
        )
        self.assertEqual(
            other.rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        scientific_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, scientific_id, 1)
        scientific = ledger.find_tax_assessment_for_draft(
            ledger.require_tenant(TENANT_ONE).tenant_account_id, scientific_id
        )
        assert scientific is not None
        ledger.tax_assessments[scientific.tax_assessment_id] = replace(
            scientific, tax_exclusive_amount="1e2"  # type: ignore[arg-type]
        )
        self.assertEqual(
            CreditAdjustmentService(ledger)
            .record_credit_adjustment(TENANT_ONE, scientific_id, Decimal("10.00"), "goodwill")
            .rejection_reason_code,
            CreditAdjustmentRejectionReasonCode.TAX_SPLIT_INVALID,
        )
        accepted = CreditAdjustmentResult(
            credit_adjustment_outcome_code=CreditAdjustmentOutcomeCode.ACCEPTED,
            credit_adjustment_contract_version=1,
            credit_adjustment_id=uuid4(),
            invoice_draft_id=uuid4(),
            tenant_reference=TENANT_ONE,
            currency_code="USD",
            credit_amount=Decimal("10.00"),
            credit_reason_code="goodwill",
            remaining_adjustable_amount=Decimal("100.00"),
            remaining_outstanding_amount=None,
            collection_case_id=None,
            collection_case_status=None,
            proposal_id=uuid4(),
            proposal_status="validated",
            source_payload_hash="sha256:" + ("a" * 64),
            idempotency_key="key",
            recorded_at=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            next_operator_action="Record the credit; AIS pulls the validated three-line unwind.",
            rejection_reason_code=None,
        )
        body = accepted.as_contract_dict()
        self.assertEqual(body["tax_exclusive_amount"], "10.00")
        self.assertEqual(body["tax_amount"], "0")
        self.assertTrue(
            any(
                "equal credit_amount" in error
                for error in validate_credit_adjustment(
                    {
                        "credit_adjustment_contract_version": 1,
                        "credit_adjustment_outcome_code": "accepted",
                        "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
                        "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
                        "credit_amount": "10.00",
                        "tax_exclusive_amount": "10.00",
                        "tax_amount": "1.00",
                        "remaining_adjustable_amount": "0",
                        "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf670",
                        "source_payload_hash": "sha256:" + ("2" * 64),
                    }
                )
            )
        )
        self.assertTrue(
            any(
                "exact decimals" in error
                for error in validate_credit_adjustment(
                    {
                        "credit_adjustment_contract_version": 1,
                        "credit_adjustment_outcome_code": "accepted",
                        "credit_adjustment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf660",
                        "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf620",
                        "credit_amount": "10.00",
                        "tax_exclusive_amount": "abc",
                        "tax_amount": "0",
                        "remaining_adjustable_amount": "0",
                        "proposal_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf670",
                        "source_payload_hash": "sha256:" + ("2" * 64),
                    }
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
