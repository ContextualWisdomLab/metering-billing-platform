"""Realistic tax-rate and tax-assessment tests for rounding, replay, and journals."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    CreditAdjustmentService,
    MemoryUsageLedger,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_journal_proposal,
    validate_tax_assessment,
    validate_tax_rate,
)
from metering_billing.errors import (
    ExactDecimalError,
    TaxAssessmentOutcomeCode,
    TaxAssessmentQueryError,
    TaxAssessmentRejectionReasonCode,
    TaxRateOutcomeCode,
    TaxRateQueryError,
    TaxRateRejectionReasonCode,
)
from metering_billing.http_app import HttpRequestError, _dispatch_write
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.tax_assessment import (
    CurrencyExponentError,
    TaxAssessmentResult,
    currency_minor_units,
    round_tax_amount,
)
from metering_billing.tax_rate import TaxRateListPage, TaxRateResult, parse_tax_rate
from metering_billing.usage_ledger import (
    StoredInvoiceDraft,
    StoredTaxAssessment,
    StoredTaxRateSchedule,
    StoredTaxRateVersion,
    generate_record_id,
)
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import seed_rated_ledger


STANDARD_TAX_RATE = Decimal("0.10")
HUNDRED = Decimal("100.00")
HUNDRED_INT = Decimal("100")


def insert_commercial_draft(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    currency_code: str,
    drafted_total: Decimal,
) -> UUID:
    """Persist one synthetic invoice draft for currency-specific tax tests."""
    tenant = ledger.require_tenant(tenant_reference)
    draft_id = generate_record_id()
    ledger.insert_invoice_draft(
        StoredInvoiceDraft(
            invoice_draft_id=draft_id,
            tenant_account_id=tenant.tenant_account_id,
            rating_run_id=generate_record_id(),
            usage_snapshot_hash="sha256:" + ("a" * 64),
            currency_code=currency_code,
            invoice_draft_status="draft",
            drafted_total_amount=drafted_total,
            recorded_at=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            invoice_draft_lines=(),
        ),
        (),
    )
    return draft_id


class TaxAssessmentTests(unittest.TestCase):
    """Verify published rates, half-even tax, journals, and collection pins."""

    def test_ten_percent_on_one_hundred_uses_currency_minor_units(self) -> None:
        """USD uses two minor units; JPY and KRW use zero (ISO 4217)."""
        ledger = seed_rated_ledger()
        published = TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        self.assertEqual(published.tax_rate_outcome_code, TaxRateOutcomeCode.ACCEPTED)
        self.assertEqual(published.tax_rate_version, 1)
        assessor = TaxAssessmentService(ledger)
        usd_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        jpy_draft = insert_commercial_draft(ledger, TENANT_ONE, "JPY", HUNDRED_INT)
        krw_draft = insert_commercial_draft(ledger, TENANT_ONE, "KRW", HUNDRED_INT)
        usd = assessor.assess_tax(TENANT_ONE, usd_draft, 1)
        jpy = assessor.assess_tax(TENANT_ONE, jpy_draft, 1)
        krw = assessor.assess_tax(TENANT_ONE, krw_draft, 1)
        self.assertEqual(usd.tax_exclusive_amount, HUNDRED)
        self.assertEqual(usd.tax_amount, Decimal("10.00"))
        self.assertEqual(usd.tax_inclusive_amount, Decimal("110.00"))
        self.assertEqual(jpy.tax_amount, Decimal("10"))
        self.assertEqual(jpy.tax_inclusive_amount, Decimal("110"))
        self.assertEqual(krw.tax_amount, Decimal("10"))
        self.assertEqual(krw.tax_inclusive_amount, Decimal("110"))
        self.assertEqual(validate_tax_rate(published.as_contract_dict()), ())
        self.assertEqual(validate_tax_assessment(usd.as_contract_dict()), ())

    def test_half_even_rounds_five_to_even(self) -> None:
        """0.05 times 10 percent is 0.005 and half-even USD rounding is 0.00."""
        self.assertEqual(round_tax_amount(Decimal("0.005"), "USD"), Decimal("0.00"))
        self.assertEqual(round_tax_amount(Decimal("1.5"), "JPY"), Decimal("2"))
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("0.05"))
        result = TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        self.assertEqual(result.tax_amount, Decimal("0.00"))
        self.assertEqual(result.tax_inclusive_amount, Decimal("0.05"))
        proposed = AccountingExportService(ledger).propose_journal(TENANT_ONE, draft_id)
        self.assertEqual(len(proposed.proposal_lines), 2)
        self.assertEqual(proposed.proposal_lines[0].debit_amount, Decimal("0.05"))
        self.assertEqual(validate_journal_proposal(proposed.as_contract_dict()), ())
        untaxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("0.05"))
        untaxed = AccountingExportService(ledger).propose_journal(TENANT_ONE, untaxed_id)
        self.assertNotEqual(proposed.source_payload_hash, untaxed.source_payload_hash)

    def test_propose_journal_is_three_line_when_taxed_and_two_line_when_not(self) -> None:
        """A taxed draft emits AR/revenue/tax_payable; an untaxed draft stays two-line."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        taxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, taxed_id, 1)
        taxed = AccountingExportService(ledger).propose_journal(TENANT_ONE, taxed_id)
        self.assertEqual(len(taxed.proposal_lines), 3)
        receivable, revenue, payable = taxed.proposal_lines
        self.assertEqual(receivable.account_role_code, "accounts_receivable")
        self.assertEqual(receivable.debit_amount, Decimal("110.00"))
        self.assertEqual(revenue.account_role_code, "usage_revenue")
        self.assertEqual(revenue.credit_amount, HUNDRED)
        self.assertEqual(payable.account_role_code, "tax_payable")
        self.assertEqual(payable.credit_amount, Decimal("10.00"))
        self.assertEqual(
            receivable.debit_amount,
            revenue.credit_amount + payable.credit_amount,
        )
        self.assertEqual(validate_journal_proposal(taxed.as_contract_dict()), ())
        self.assertIn(":invoice_draft:", taxed.idempotency_key)
        untaxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        untaxed = AccountingExportService(ledger).propose_journal(TENANT_ONE, untaxed_id)
        self.assertEqual(len(untaxed.proposal_lines), 2)
        self.assertNotEqual(taxed.source_payload_hash, untaxed.source_payload_hash)

    def test_collection_uses_inclusive_amount_and_rejects_tax_after_open(self) -> None:
        """Open collection after tax uses 110.00; assess after open fails closed."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        taxed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, taxed_id, 1)
        opened = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, taxed_id)
        self.assertEqual(opened.outstanding_amount, Decimal("110.00"))
        late_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, late_id)
        late = TaxAssessmentService(ledger).assess_tax(TENANT_ONE, late_id, 1)
        self.assertEqual(
            late.rejection_reason_code,
            TaxAssessmentRejectionReasonCode.TAX_AFTER_COLLECTION_OPENED,
        )

    def test_credit_remaining_adjustable_is_inclusive(self) -> None:
        """Credits cap at tax-inclusive remaining and unwind tax_payable."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("10.00"), "goodwill"
        )
        self.assertEqual(credit.remaining_adjustable_amount, Decimal("100.00"))
        self.assertEqual(credit.tax_exclusive_amount + credit.tax_amount, credit.credit_amount)
        proposal = ledger.get_journal_proposal(credit.proposal_id)
        self.assertEqual(len(proposal.proposal_lines), 3)
        self.assertEqual(
            {line.account_role_code for line in proposal.proposal_lines},
            {"usage_revenue", "tax_payable", "accounts_receivable"},
        )

    def test_replay_and_version_increment(self) -> None:
        """Same tenant, code, rate, and contract version reuse the stored version."""
        ledger = seed_rated_ledger()
        service = TaxRateService(ledger)
        first = service.publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        replay = service.publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        second = service.publish_tax_rate(TENANT_ONE, "vat", Decimal("0.20"))
        self.assertEqual(replay.tax_rate_outcome_code, TaxRateOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(replay.tax_rate_version_id, first.tax_rate_version_id)
        self.assertEqual(second.tax_rate_outcome_code, TaxRateOutcomeCode.ACCEPTED)
        self.assertEqual(second.tax_rate_version, 2)
        self.assertEqual(second.tax_rate_schedule_id, first.tax_rate_schedule_id)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        assessor = TaxAssessmentService(ledger)
        assessed = assessor.assess_tax(TENANT_ONE, draft_id, first.tax_rate_version_id)
        again = assessor.assess_tax(TENANT_ONE, draft_id, first.tax_rate_version_id)
        self.assertEqual(again.tax_assessment_outcome_code, TaxAssessmentOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(again.tax_assessment_id, assessed.tax_assessment_id)

    def test_fail_closed_inputs_and_cross_tenant_reads(self) -> None:
        """Missing tenant, float rates, unknown currency, and cross-tenant reads reject."""
        ledger = seed_rated_ledger()
        rates = TaxRateService(ledger)
        missing_tenant = rates.publish_tax_rate("", "vat", STANDARD_TAX_RATE)
        unknown_tenant = rates.publish_tax_rate("urn:cwl:missing_tenant", "vat", STANDARD_TAX_RATE)
        bad_code = rates.publish_tax_rate(TENANT_ONE, "excise", STANDARD_TAX_RATE)
        floated = rates.publish_tax_rate(TENANT_ONE, "vat", 0.10)
        flagged = rates.publish_tax_rate(TENANT_ONE, "vat", True)
        percent = rates.publish_tax_rate(TENANT_ONE, "vat", 10)
        over = rates.publish_tax_rate(TENANT_ONE, "vat", Decimal("1.01"))
        self.assertEqual(missing_tenant.rejection_reason_code, TaxRateRejectionReasonCode.TENANT_NOT_FOUND)
        self.assertEqual(unknown_tenant.rejection_reason_code, TaxRateRejectionReasonCode.TENANT_NOT_FOUND)
        self.assertEqual(bad_code.rejection_reason_code, TaxRateRejectionReasonCode.TAX_CODE_INVALID)
        self.assertEqual(floated.rejection_reason_code, TaxRateRejectionReasonCode.TAX_RATE_INVALID)
        self.assertEqual(flagged.rejection_reason_code, TaxRateRejectionReasonCode.TAX_RATE_INVALID)
        self.assertEqual(percent.rejection_reason_code, TaxRateRejectionReasonCode.TAX_RATE_INVALID)
        self.assertEqual(over.rejection_reason_code, TaxRateRejectionReasonCode.TAX_RATE_INVALID)
        rates.publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        assessor = TaxAssessmentService(ledger)
        self.assertEqual(
            assessor.assess_tax("", uuid4(), 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            assessor.assess_tax("urn:cwl:missing_tenant", uuid4(), 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, "not-a-uuid", 1).rejection_reason_code,  # type: ignore[arg-type]
            TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, uuid4(), 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        other_draft = insert_commercial_draft(ledger, TENANT_TWO, "USD", HUNDRED)
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, other_draft, 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND,
        )
        own_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, own_draft, 9).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.TAX_RATE_NOT_FOUND,
        )
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, own_draft, True).rejection_reason_code,  # type: ignore[arg-type]
            TaxAssessmentRejectionReasonCode.TAX_RATE_NOT_FOUND,
        )
        zero_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("0"))
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, zero_draft, 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.DRAFT_TOTAL_INVALID,
        )
        xxx_draft = insert_commercial_draft(ledger, TENANT_ONE, "XXX", HUNDRED)
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, xxx_draft, 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.CURRENCY_EXPONENT_UNKNOWN,
        )
        other_rate = rates.publish_tax_rate(TENANT_TWO, "vat", STANDARD_TAX_RATE)
        self.assertEqual(
            assessor.assess_tax(
                TENANT_ONE, own_draft, other_rate.tax_rate_version_id
            ).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.TAX_RATE_NOT_FOUND,
        )
        scientific_id = generate_record_id()
        tenant = ledger.require_tenant(TENANT_ONE)
        ledger.insert_invoice_draft(
            StoredInvoiceDraft(
                invoice_draft_id=scientific_id,
                tenant_account_id=tenant.tenant_account_id,
                rating_run_id=generate_record_id(),
                usage_snapshot_hash="sha256:" + ("b" * 64),
                currency_code="USD",
                invoice_draft_status="draft",
                drafted_total_amount="1e2",  # type: ignore[arg-type]
                recorded_at=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
                invoice_draft_lines=(),
            ),
            (),
        )
        self.assertEqual(
            assessor.assess_tax(TENANT_ONE, scientific_id, 1).rejection_reason_code,
            TaxAssessmentRejectionReasonCode.DRAFT_TOTAL_INVALID,
        )
        first = rates.publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        with self.assertRaises(TaxRateQueryError) as other_rate:
            TaxRateService(ledger).get_tax_rate_version(TENANT_TWO, first.tax_rate_version_id)
        self.assertEqual(other_rate.exception.rejection_reason_code, "tax_rate_not_found")
        assessed = assessor.assess_tax(TENANT_ONE, own_draft, 1)
        with self.assertRaises(TaxAssessmentQueryError) as other_assessment:
            assessor.get_tax_assessment(TENANT_TWO, assessed.tax_assessment_id)
        self.assertEqual(other_assessment.exception.rejection_reason_code, "tax_assessment_not_found")
        with self.assertRaises(ExactDecimalError):
            parse_tax_rate(0.10)
        with self.assertRaises(ExactDecimalError):
            parse_tax_rate(True)
        with self.assertRaises(CurrencyExponentError):
            currency_minor_units("XXX")

    def test_http_publish_list_assess_and_get_are_tenant_scoped(self) -> None:
        """Operators POST a rate and assessment; GET stays on the same tenant."""
        ledger = seed_rated_ledger()
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/tax-rates",
            {"tenant_reference": TENANT_ONE, "tax_code": "vat", "tax_rate": "0.10"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["tax_rate_outcome_code"], "accepted")
        self.assertEqual(validate_tax_rate(body), ())
        version_id = body["tax_rate_version_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/tax-rates",
            {"tax_code": "vat", "tax_rate": "0.10"},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["tax_rate_outcome_code"], "duplicate_replay")
        list_status, list_body = invoke_http(
            app, "GET", "/v1/tax-rates", query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(list_body["tax_rates"][0]["tax_code"], "vat")
        number_status, number_body = invoke_http(
            app, "GET", "/v1/tax-rate-versions/1", query={"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(number_status, 200)
        self.assertEqual(number_body["tax_rate_version"], 1)
        version_status, version_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-rate-versions/{version_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(version_status, 200)
        assess_status, assess_body = invoke_http(
            app,
            "POST",
            "/v1/tax-assessments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft_id),
                "tax_rate_version": 1,
            },
        )
        self.assertEqual(assess_status, 200)
        self.assertEqual(assess_body["tax_inclusive_amount"], "110.00")
        self.assertEqual(validate_tax_assessment(assess_body), ())
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{assess_body['tax_assessment_id']}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{assess_body['tax_assessment_id']}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "tax_assessment_not_found")
        other_rate_status, other_rate_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-rate-versions/{version_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_rate_status, 404)
        missing_status, missing_body = invoke_http(
            app, "POST", "/v1/tax-rates", {"tax_code": "vat", "tax_rate": "0.10"}
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        pin_status, pin_body = invoke_http(
            app,
            "POST",
            "/v1/tax-rates",
            {"tenant_reference": TENANT_ONE, "tax_code": "vat", "tax_rate": "0.10"},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(pin_status, 422)
        self.assertEqual(pin_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(app, "PUT", "/v1/tax-rates")
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        list_missing, list_missing_body = invoke_http(app, "GET", "/v1/tax-rates")
        self.assertEqual(list_missing, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")

    def test_helpers_and_ledger_identity_fail_closed(self) -> None:
        """Helpers and ledger inserts reject conflicting tax identities."""
        self.assertEqual(parse_tax_rate(Decimal("0")), Decimal("0"))
        self.assertEqual(parse_tax_rate("0.10"), STANDARD_TAX_RATE)
        self.assertEqual(parse_tax_rate(1), Decimal("1"))
        self.assertEqual(parse_tax_rate(0), Decimal("0"))
        self.assertEqual(currency_minor_units("USD"), 2)
        self.assertEqual(currency_minor_units("JPY"), 0)
        empty = TaxRateService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        self.assertEqual(
            TaxRateListPage(tenant_reference=TENANT_ONE, tax_rates=()).as_contract_dict(),
            {"tenant_reference": TENANT_ONE, "tax_rates": []},
        )
        rejected = TaxRateResult(
            tax_rate_outcome_code=TaxRateOutcomeCode.REJECTED,
            tax_rate_contract_version=1,
            tax_rate_schedule_id=None,
            tax_rate_version_id=None,
            tenant_reference=None,
            tax_code=None,
            tax_rate_version=None,
            tax_rate=None,
            source_payload_hash=None,
            published_at=None,
            next_operator_action="Publish a tax rate, assess the draft, then propose the journal and let AIS pull.",
            rejection_reason_code=None,
        )
        self.assertEqual(rejected.as_contract_dict()["rejection_reason_code"], "tax_rate_not_found")
        with self.assertRaises(ValueError):
            TaxRateResult(
                tax_rate_outcome_code="nope",  # type: ignore[arg-type]
                tax_rate_contract_version=1,
                tax_rate_schedule_id=None,
                tax_rate_version_id=None,
                tenant_reference=None,
                tax_code=None,
                tax_rate_version=None,
                tax_rate=None,
                source_payload_hash=None,
                published_at=None,
                next_operator_action="x",
                rejection_reason_code=None,
            ).as_contract_dict()
        with self.assertRaises(ValueError):
            TaxRateResult(
                tax_rate_outcome_code=TaxRateOutcomeCode.ACCEPTED,
                tax_rate_contract_version=1,
                tax_rate_schedule_id=None,
                tax_rate_version_id=None,
                tenant_reference=None,
                tax_code=None,
                tax_rate_version=None,
                tax_rate=None,
                source_payload_hash=None,
                published_at=None,
                next_operator_action="x",
                rejection_reason_code=None,
            ).as_contract_dict()
        rejected_assessment = TaxAssessmentResult(
            tax_assessment_outcome_code=TaxAssessmentOutcomeCode.REJECTED,
            tax_assessment_contract_version=1,
            tax_assessment_id=None,
            tenant_reference=None,
            invoice_draft_id=None,
            tax_rate_version_id=None,
            tax_rate_version=None,
            tax_code=None,
            tax_rate=None,
            currency_code=None,
            tax_exclusive_amount=None,
            tax_amount=None,
            tax_inclusive_amount=None,
            source_payload_hash=None,
            assessed_at=None,
            next_operator_action="x",
            rejection_reason_code=None,
        )
        self.assertEqual(
            rejected_assessment.as_contract_dict()["rejection_reason_code"],
            "tax_assessment_not_found",
        )
        with self.assertRaises(ValueError):
            TaxAssessmentResult(
                tax_assessment_outcome_code="nope",  # type: ignore[arg-type]
                tax_assessment_contract_version=1,
                tax_assessment_id=None,
                tenant_reference=None,
                invoice_draft_id=None,
                tax_rate_version_id=None,
                tax_rate_version=None,
                tax_code=None,
                tax_rate=None,
                currency_code=None,
                tax_exclusive_amount=None,
                tax_amount=None,
                tax_inclusive_amount=None,
                source_payload_hash=None,
                assessed_at=None,
                next_operator_action="x",
                rejection_reason_code=None,
            ).as_contract_dict()
        with self.assertRaises(ValueError):
            TaxAssessmentResult(
                tax_assessment_outcome_code=TaxAssessmentOutcomeCode.ACCEPTED,
                tax_assessment_contract_version=1,
                tax_assessment_id=None,
                tenant_reference=None,
                invoice_draft_id=None,
                tax_rate_version_id=None,
                tax_rate_version=None,
                tax_code=None,
                tax_rate=None,
                currency_code=None,
                tax_exclusive_amount=None,
                tax_amount=None,
                tax_inclusive_amount=None,
                source_payload_hash=None,
                assessed_at=None,
                next_operator_action="x",
                rejection_reason_code=None,
            ).as_contract_dict()
        self.assertTrue(validate_tax_rate(["not a mapping"]))
        self.assertTrue(validate_tax_rate({"tax_rate_contract_version": 1}))
        self.assertTrue(
            validate_tax_rate(
                {"tax_rate_contract_version": 1, "tax_rate_outcome_code": "accepted"}
            )
        )
        self.assertEqual(
            validate_tax_rate(
                {
                    "tax_rate_contract_version": 1,
                    "tax_rate_outcome_code": "rejected",
                    "rejection_reason_code": "tenant_not_found",
                }
            ),
            (),
        )
        self.assertTrue(
            validate_tax_rate(
                {"tax_rate_contract_version": 1, "tax_rate_outcome_code": "rejected"}
            )
        )
        self.assertTrue(validate_tax_assessment(["not a mapping"]))
        self.assertTrue(validate_tax_assessment({"tax_assessment_contract_version": 1}))
        self.assertTrue(
            validate_tax_assessment(
                {"tax_assessment_contract_version": 1, "tax_assessment_outcome_code": "accepted"}
            )
        )
        self.assertTrue(
            validate_tax_assessment(
                {
                    "tax_assessment_contract_version": 1,
                    "tax_assessment_outcome_code": "accepted",
                    "tax_exclusive_amount": "abc",
                    "tax_amount": "10.00",
                    "tax_inclusive_amount": "110.00",
                }
            )
        )
        self.assertEqual(
            validate_tax_assessment(
                {
                    "tax_assessment_contract_version": 1,
                    "tax_assessment_outcome_code": "rejected",
                    "rejection_reason_code": "tenant_not_found",
                }
            ),
            (),
        )
        self.assertTrue(
            validate_tax_assessment(
                {"tax_assessment_contract_version": 1, "tax_assessment_outcome_code": "rejected"}
            )
        )
        self.assertTrue(
            any(
                "tax_inclusive" in error
                for error in validate_tax_assessment(
                    {
                        "tax_assessment_contract_version": 1,
                        "tax_assessment_outcome_code": "accepted",
                        "tax_assessment_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf690",
                        "invoice_draft_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf691",
                        "tax_rate_version_id": "019d7b92-1aa0-7a7f-b61c-962c0f4bf692",
                        "tax_exclusive_amount": "100.00",
                        "tax_amount": "10.00",
                        "tax_inclusive_amount": "109.00",
                        "source_payload_hash": "sha256:" + ("3" * 64),
                    }
                )
            )
        )

        ledger = seed_rated_ledger()
        tenant = ledger.require_tenant(TENANT_ONE)
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_schedule(
                StoredTaxRateSchedule(
                    tax_rate_schedule_id=generate_record_id(),
                    tenant_account_id=tenant.tenant_account_id,
                    tax_code="excise",
                    created_at=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
                )
            )
        first = TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        schedule = ledger.get_tax_rate_schedule(first.tax_rate_schedule_id)
        replay_schedule = ledger.insert_tax_rate_schedule(schedule)
        self.assertEqual(replay_schedule.tax_rate_schedule_id, schedule.tax_rate_schedule_id)
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_schedule(replace(schedule, tax_code="gst"))
        version = ledger.get_tax_rate_version(first.tax_rate_version_id)
        again = ledger.insert_tax_rate_version(version)
        self.assertEqual(again.tax_rate_version_id, version.tax_rate_version_id)
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_version(replace(version, version_number=0))
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_version(replace(version, tax_code="excise"))
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_version(replace(version, tax_rate=Decimal("1.5")))
        with self.assertRaises(ValueError):
            ledger.insert_tax_rate_version(
                replace(
                    version,
                    source_payload_hash="sha256:" + ("b" * 64),
                )
            )
        header_only = ledger.insert_tax_rate_schedule(
            StoredTaxRateSchedule(
                tax_rate_schedule_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                tax_code="gst",
                created_at=datetime(2026, 8, 17, 21, 1, tzinfo=UTC),
            )
        )
        listed = TaxRateService(ledger).list_tax_rates(TENANT_ONE)
        self.assertIn(None, [item["latest_tax_rate_version"] for item in listed.tax_rates])
        self.assertIsNone(
            ledger.find_tax_rate_version(tenant.tenant_account_id, 1, "missing")
        )
        self.assertIsNone(
            ledger.find_tax_rate_version(tenant.tenant_account_id, 9, "vat")
        )
        self.assertEqual(
            ledger.list_tax_rate_versions(tenant.tenant_account_id, None)[0].version_number,
            1,
        )
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).get_tax_rate_version(TENANT_ONE, True)  # type: ignore[arg-type]
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).get_tax_rate_version("", first.tax_rate_version_id)
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).get_tax_rate_version(
                "urn:cwl:missing_tenant", first.tax_rate_version_id
            )
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).list_tax_rates("")
        orphan = ledger.insert_tax_rate_version(
            StoredTaxRateVersion(
                tax_rate_version_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                tax_rate_schedule_id=uuid4(),
                version_number=7,
                tax_rate_contract_version=1,
                tax_code="vat",
                tax_rate=STANDARD_TAX_RATE,
                source_payload_hash="sha256:" + ("c" * 64),
                published_at=datetime(2026, 8, 17, 21, 2, tzinfo=UTC),
            )
        )
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).get_tax_rate_version(TENANT_ONE, orphan.tax_rate_version_id)
        other = ledger.require_tenant(TENANT_TWO)
        foreign = ledger.insert_tax_rate_version(
            StoredTaxRateVersion(
                tax_rate_version_id=generate_record_id(),
                tenant_account_id=other.tenant_account_id,
                tax_rate_schedule_id=schedule.tax_rate_schedule_id,
                version_number=9,
                tax_rate_contract_version=1,
                tax_code="vat",
                tax_rate=STANDARD_TAX_RATE,
                source_payload_hash="sha256:" + ("d" * 64),
                published_at=datetime(2026, 8, 17, 21, 3, tzinfo=UTC),
            )
        )
        with self.assertRaises(TaxRateQueryError):
            TaxRateService(ledger).get_tax_rate_version(TENANT_TWO, foreign.tax_rate_version_id)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        assessed = TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        stored = ledger.get_tax_assessment(assessed.tax_assessment_id)
        replay_assessment = ledger.insert_tax_assessment(stored)
        self.assertEqual(replay_assessment.tax_assessment_id, stored.tax_assessment_id)
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(replace(stored, tax_code="excise"))
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(replace(stored, tax_exclusive_amount=Decimal("0")))
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(replace(stored, tax_inclusive_amount=Decimal("1")))
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(replace(stored, tax_rate=Decimal("1.5")))
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(
                replace(
                    stored,
                    tax_rate_version_id=uuid4(),
                    source_payload_hash="sha256:" + ("e" * 64),
                )
            )
        other_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("50.00"))
        with self.assertRaises(ValueError):
            ledger.insert_tax_assessment(
                replace(
                    stored,
                    invoice_draft_id=other_draft,
                    source_payload_hash="sha256:" + ("f" * 64),
                )
            )
        with self.assertRaises(TaxAssessmentQueryError):
            TaxAssessmentService(ledger).get_tax_assessment("", assessed.tax_assessment_id)
        with self.assertRaises(TaxAssessmentQueryError):
            TaxAssessmentService(ledger).get_tax_assessment(TENANT_ONE, None)  # type: ignore[arg-type]
        with self.assertRaises(TaxAssessmentQueryError):
            TaxAssessmentService(ledger).get_tax_assessment(
                "urn:cwl:missing_tenant", assessed.tax_assessment_id
            )
        with self.assertRaises(TaxAssessmentQueryError):
            TaxAssessmentService(ledger).get_tax_assessment(TENANT_ONE, uuid4())
        self.assertIsNone(
            ledger.find_tax_assessment_for_draft(tenant.tenant_account_id, uuid4())
        )
        gst = TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "gst", STANDARD_TAX_RATE)
        self.assertEqual(gst.tax_rate_version, 1)
        self.assertIsNone(ledger.find_tax_rate_version(tenant.tenant_account_id, 1))
        named = ledger.find_tax_rate_version(tenant.tenant_account_id, 1, "vat")
        self.assertEqual(named.tax_rate_version_id, first.tax_rate_version_id)
        with self.assertRaises(HttpRequestError) as missing_rates:
            _dispatch_write(
                "tax_rates",
                {},
                TENANT_ONE,
                {"tax_code": "vat", "tax_rate": "0.10"},
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
            )
        self.assertEqual(missing_rates.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(HttpRequestError) as missing_assessments:
            _dispatch_write(
                "tax_assessments",
                {},
                TENANT_ONE,
                {"invoice_draft_id": str(draft_id), "tax_rate_version": 1},
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,
                None,
                TaxRateService(ledger),
            )
        self.assertEqual(missing_assessments.exception.rejection_reason_code, "request_invalid")
        app = create_http_app(ledger)
        uuid_assess_status, uuid_assess_body = invoke_http(
            app,
            "POST",
            "/v1/tax-assessments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(
                    insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
                ),
                "tax_rate_version": str(first.tax_rate_version_id),
            },
        )
        self.assertEqual(uuid_assess_status, 200)
        bad_version_status, bad_version_body = invoke_http(
            app,
            "POST",
            "/v1/tax-assessments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft_id),
                "tax_rate_version": True,
            },
        )
        self.assertEqual(bad_version_status, 422)
        method_version_status, method_version_body = invoke_http(
            app, "PUT", "/v1/tax-rate-versions/1"
        )
        self.assertEqual(method_version_status, 422)
        method_assessment_status, method_assessment_body = invoke_http(
            app, "PUT", f"/v1/tax-assessments/{assessed.tax_assessment_id}"
        )
        self.assertEqual(method_assessment_status, 422)
        collection_get_status, collection_get_body = invoke_http(app, "GET", "/v1/tax-assessments")
        self.assertEqual(collection_get_status, 422)
        self.assertEqual(collection_get_body["rejection_reason_code"], "request_invalid")
        bad_uuid_status, bad_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments/" + ("-" * 36),
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bad_uuid_status, 422)
        self.assertEqual(bad_uuid_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.TaxRateService.get_tax_rate_version",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/tax-rate-versions/{first.tax_rate_version_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        with mock.patch(
            "metering_billing.http_app.TaxAssessmentService.get_tax_assessment",
            side_effect=ValueError("closed"),
        ):
            assess_value_status, assess_value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/tax-assessments/{assessed.tax_assessment_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(assess_value_status, 422)
        empty_assess = TaxAssessmentService()
        self.assertIsInstance(empty_assess.ledger, MemoryUsageLedger)
        morning_ledger, morning_draft = draft_known_morning()
        TaxRateService(morning_ledger).publish_tax_rate(TENANT_ONE, "sales_tax", STANDARD_TAX_RATE)
        morning = TaxAssessmentService(morning_ledger).assess_tax(TENANT_ONE, morning_draft, 1)
        self.assertEqual(morning.tax_assessment_outcome_code, TaxAssessmentOutcomeCode.ACCEPTED)
        self.assertEqual(parse_invoice_amount(morning.tax_exclusive_amount), morning.tax_exclusive_amount)


if __name__ == "__main__":
    unittest.main()
