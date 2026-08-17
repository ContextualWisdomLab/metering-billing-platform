"""Tax-assessment HTTP presentment tests for tenant-scoped tax reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    TaxAssessmentPresentmentService,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
)
from metering_billing.contracts import validate_tax_assessment_presentment
from metering_billing.errors import TaxAssessmentPresentmentQueryError
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.tax_assessment_presentment import next_operator_action
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import seed_rated_ledger


def publish_vat(ledger):
    """Publish the known 10 percent VAT card used by #19 tests."""
    return TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)


def assess_named_draft(ledger, drafted_total=HUNDRED, clock=None):
    """Assess one #19 draft and return the stored result."""
    draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", drafted_total)
    service = (
        TaxAssessmentService(ledger, clock=clock)
        if clock is not None
        else TaxAssessmentService(ledger)
    )
    result = service.assess_tax(TENANT_ONE, draft_id, 1)
    assert result.tax_assessment_id is not None
    return result


class TaxAssessmentPresentmentTests(unittest.TestCase):
    """Verify stored-tax GET, list envelope, and fail-closed isolation."""

    def test_stored_assessment_projects_amounts_and_propose_journal(self) -> None:
        """A stored 10 percent assessment shows exact money and propose_journal."""
        ledger = seed_rated_ledger()
        publish_vat(ledger)
        assessed = assess_named_draft(ledger)
        first = TaxAssessmentPresentmentService(ledger).present_tax_assessment(
            TENANT_ONE, assessed.tax_assessment_id
        )
        second = TaxAssessmentPresentmentService(ledger).present_tax_assessment(
            TENANT_ONE, assessed.tax_assessment_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.tax_assessment_id, assessed.tax_assessment_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.invoice_draft_id, assessed.invoice_draft_id)
        self.assertEqual(first.tax_code, "vat")
        self.assertEqual(first.tax_rate, STANDARD_TAX_RATE)
        self.assertEqual(first.tax_exclusive_amount, HUNDRED)
        self.assertEqual(first.tax_amount, Decimal("10.00"))
        self.assertEqual(first.tax_inclusive_amount, Decimal("110.00"))
        self.assertEqual(first.next_operator_action, "propose_journal")
        payload = first.as_contract_dict()
        self.assertEqual(validate_tax_assessment_presentment(payload), ())
        self.assertIsInstance(payload["tax_inclusive_amount"], str)
        self.assertNotIsInstance(payload["tax_inclusive_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("tax_assessment_outcome_code", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("tax_assessment_status", payload)

    def test_http_get_keeps_item_and_adds_list_envelope(self) -> None:
        """Item GET stays #19; list uses {tax_assessments, next_cursor}."""
        ledger = seed_rated_ledger()
        publish_vat(ledger)
        first = assess_named_draft(
            ledger,
            clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
        )
        second = assess_named_draft(
            ledger,
            drafted_total=Decimal("20.00"),
            clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{first.tax_assessment_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["tax_assessment_id"], str(first.tax_assessment_id))
        self.assertEqual(body["tax_inclusive_amount"], "110.00")
        self.assertEqual(body["invoice_draft_id"], str(first.invoice_draft_id))
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{first.tax_assessment_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"tax_assessments", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["tax_assessments"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["tax_assessments"][0]
        self.assertEqual(
            set(first_summary),
            {
                "tax_assessment_id",
                "invoice_draft_id",
                "tax_inclusive_amount",
                "assessed_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["tax_assessments"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["tax_assessment_id"],
            second_body["tax_assessments"][0]["tax_assessment_id"],
        }
        self.assertEqual(
            listed_ids, {str(first.tax_assessment_id), str(second.tax_assessment_id)}
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["tax_assessments"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_assess_and_refuses_card_data(self) -> None:
        """POST stays the #19 assess command; PAN and secrets are refused."""
        ledger = seed_rated_ledger()
        publish_vat(ledger)
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        app = create_http_app(ledger)
        payload = {
            "tenant_reference": TENANT_ONE,
            "invoice_draft_id": str(draft_id),
            "tax_rate_version": 1,
        }
        status, body = invoke_http(
            app,
            "POST",
            "/v1/tax-assessments",
            {**payload, "card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.tax_assessments), 0)
        accepted_status, accepted_body = invoke_http(
            app, "POST", "/v1/tax-assessments", payload
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["tax_assessment_outcome_code"], "accepted")
        self.assertIn("tax_assessment_id", accepted_body)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no money."""
        ledger = seed_rated_ledger()
        publish_vat(ledger)
        assessed = assess_named_draft(ledger)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/tax-assessments/{assessed.tax_assessment_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{assessed.tax_assessment_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "tax_assessment_not_found")
        self.assertNotIn("tax_inclusive_amount", other_body)
        self.assertNotIn("tax_amount", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/tax-assessments/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "tax_assessment_not_found")
        with self.assertRaises(TaxAssessmentPresentmentQueryError) as crossed:
            TaxAssessmentPresentmentService(ledger).present_tax_assessment(
                TENANT_TWO, assessed.tax_assessment_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "tax_assessment_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and propose_journal."""
        self.assertEqual(next_operator_action(), "propose_journal")
        ledger = seed_rated_ledger()
        publish_vat(ledger)
        assessed = assess_named_draft(ledger)
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/tax-assessments",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/tax-assessments/{assessed.tax_assessment_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.TaxAssessmentPresentmentService.list_tax_assessments",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/tax-assessments",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = TaxAssessmentPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            empty.list_tax_assessments(TENANT_ONE)
        service = TaxAssessmentPresentmentService(ledger)
        listed = service.list_tax_assessments(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.tax_assessments), 1)
        self.assertEqual(
            ledger.get_tax_assessment(assessed.tax_assessment_id).tax_assessment_id,
            assessed.tax_assessment_id,
        )
        self.assertEqual(
            len(
                ledger.list_tax_assessments(
                    ledger.require_tenant(TENANT_ONE).tenant_account_id
                )
            ),
            1,
        )
        self.assertEqual(len(ledger.list_tax_assessments()), 1)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.list_tax_assessments(TENANT_ONE, page_limit=True)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.list_tax_assessments(TENANT_ONE, page_limit=101)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.list_tax_assessments(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.list_tax_assessments(TENANT_ONE, page_limit="abc")
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.list_tax_assessments(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/tax-assessments")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_tax_assessments(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.tax_assessments), 1)
        empty_limit = service.list_tax_assessments(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.tax_assessments), 1)
        self.assertEqual(service.list_tax_assessments(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.present_tax_assessment(TENANT_ONE, uuid4())
        with self.assertRaises(TaxAssessmentPresentmentQueryError):
            service.present_tax_assessment("", assessed.tax_assessment_id)
        self.assertIsNone(ledger.get_tax_assessment(uuid4()))
        self.assertEqual(
            format_exact_decimal(listed.tax_assessments[0].tax_inclusive_amount),
            "110.00",
        )
