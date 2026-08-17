"""Invoice-draft presentment tests for tenant-scoped statement reads."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    CreditAdjustmentService,
    InvoiceDraftService,
    TaxAssessmentService,
    TaxRateService,
    UsageRatingService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_invoice_presentment
from metering_billing.errors import ExactDecimalError, InvoicePresentmentQueryError
from metering_billing.invoice_presentment import (
    InvoicePresentmentService,
    remaining_amount_due,
)
from metering_billing.usage_ledger import (
    StoredCreditAdjustment,
    StoredInvoiceDraft,
    StoredInvoiceDraftLine,
    generate_record_id,
)
from test_http_app import invoke_http
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import (
    KNOWN_MORNING_QUANTITY,
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    TOKEN_UNIT_PRICE,
    ingest_known_batch,
    seed_rated_ledger,
)


class InvoicePresentmentTests(unittest.TestCase):
    """Verify projected statements, HTTP GET, and fail-closed tenant isolation."""

    def test_taxed_partial_credit_presentment_shows_inclusive_minus_credit(self) -> None:
        """Rated, taxed, and partially credited drafts must show amount_due = inclusive - credit."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
        CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("11.00"), "goodwill"
        )
        first = InvoicePresentmentService(ledger).present_invoice_draft(TENANT_ONE, draft_id)
        second = InvoicePresentmentService(ledger).present_invoice_draft(TENANT_ONE, draft_id)
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.tax_exclusive_amount, HUNDRED)
        self.assertEqual(first.tax_amount, Decimal("10.00"))
        self.assertEqual(first.tax_inclusive_amount, Decimal("110.00"))
        self.assertEqual(first.credited_amount, Decimal("11.00"))
        self.assertEqual(first.amount_due, Decimal("99.00"))
        self.assertEqual(
            first.tax_exclusive_amount + first.tax_amount, first.tax_inclusive_amount
        )
        self.assertEqual(first.amount_due, first.tax_inclusive_amount - first.credited_amount)
        self.assertIsNotNone(first.collection_case_id)
        self.assertEqual(first.collection_outstanding, Decimal("99.00"))
        self.assertEqual(len(first.invoice_lines), 1)
        line = first.invoice_lines[0]
        self.assertEqual(line.metric_code, "gen_ai_output_token")
        self.assertEqual(line.quantity, Decimal("100"))
        self.assertEqual(line.unit_amount, Decimal("1.00"))
        self.assertEqual(line.line_amount, HUNDRED)
        payload = first.as_contract_dict()
        self.assertEqual(validate_invoice_presentment(payload), ())
        self.assertIsInstance(payload["amount_due"], str)
        self.assertNotIsInstance(payload["amount_due"], float)
        self.assertIsInstance(payload["invoice_lines"][0]["quantity"], str)
        self.assertNotIsInstance(payload["invoice_lines"][0]["quantity"], float)

    def test_untaxed_known_morning_presentment_zeros_tax_and_copies_lines(self) -> None:
        """An untaxed rated draft keeps tax at zero and projects rating line fields."""
        ingest = ingest_known_batch()
        rating = UsageRatingService(ingest.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1
        )
        draft = InvoiceDraftService(ingest.ledger).draft_invoice(
            TENANT_ONE, rating.rating_run_id
        )
        assert draft.invoice_draft_id is not None
        statement = InvoicePresentmentService(ingest.ledger).present_invoice_draft(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(statement.rating_run_id, rating.rating_run_id)
        self.assertEqual(statement.tax_exclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(statement.tax_amount, Decimal("0"))
        self.assertEqual(statement.tax_inclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(statement.credited_amount, Decimal("0"))
        self.assertEqual(statement.amount_due, KNOWN_MORNING_TOTAL)
        self.assertIsNone(statement.collection_case_id)
        self.assertIsNone(statement.collection_outstanding)
        self.assertNotIn("collection_case_id", statement.as_contract_dict())
        line = statement.invoice_lines[0]
        self.assertEqual(line.metric_code, "gen_ai_output_token")
        self.assertEqual(line.quantity, KNOWN_MORNING_QUANTITY)
        self.assertEqual(line.unit_amount, TOKEN_UNIT_PRICE)
        self.assertEqual(line.line_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(validate_invoice_presentment(statement.as_contract_dict()), ())

    def test_http_get_returns_statement_and_list_summaries(self) -> None:
        """Same-tenant GET is 200; list uses the journal-proposal cursor envelope."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        first_id = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        second_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, first_id, 1)
        CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, first_id, Decimal("11.00"), "goodwill"
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/invoice-drafts/{first_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["invoice_draft_id"], str(first_id))
        self.assertEqual(body["tax_inclusive_amount"], "110.00")
        self.assertEqual(body["credited_amount"], "11.00")
        self.assertEqual(body["amount_due"], "99.00")
        self.assertEqual(validate_invoice_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/invoice-drafts/{first_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body["invoice_drafts"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["invoice_drafts"][0]
        self.assertEqual(
            set(first_summary),
            {"invoice_draft_id", "amount_due", "currency_code", "drafted_at"},
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["invoice_drafts"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["invoice_draft_id"],
            second_body["invoice_drafts"][0]["invoice_draft_id"],
        }
        self.assertEqual(listed_ids, {str(first_id), str(second_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["invoice_drafts"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no statement."""
        ledger = seed_rated_ledger()
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(app, "GET", f"/v1/invoice-drafts/{draft_id}")
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/invoice-drafts/{draft_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "invoice_draft_not_found")
        self.assertNotIn("amount_due", other_body)
        self.assertNotIn("invoice_lines", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/invoice-drafts/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "invoice_draft_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            f"/v1/invoice-drafts/{draft_id}",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        with self.assertRaises(InvoicePresentmentQueryError) as missing_tenant:
            InvoicePresentmentService(ledger).present_invoice_draft("", draft_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(InvoicePresentmentQueryError) as crossed:
            InvoicePresentmentService(ledger).present_invoice_draft(TENANT_TWO, draft_id)
        self.assertEqual(crossed.exception.rejection_reason_code, "invoice_draft_not_found")

    def test_amount_due_never_goes_below_zero_and_filters_fail_closed(self) -> None:
        """Over-credited ledgers clamp due at zero; illegal list filters stay 422."""
        self.assertEqual(remaining_amount_due(Decimal("10.00"), Decimal("11.00")), Decimal("0"))
        self.assertEqual(remaining_amount_due(Decimal("10.00"), Decimal("4.00")), Decimal("6.00"))
        ledger = seed_rated_ledger()
        draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        tenant = ledger.require_tenant(TENANT_ONE)
        ledger.insert_credit_adjustment(
            StoredCreditAdjustment(
                credit_adjustment_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=draft_id,
                credit_adjustment_contract_version=1,
                credit_reason_code="goodwill",
                currency_code="USD",
                credit_amount=Decimal("150.00"),
                tax_exclusive_amount=Decimal("150.00"),
                tax_amount=Decimal("0"),
                source_payload_hash="sha256:" + ("b" * 64),
                recorded_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
            )
        )
        statement = InvoicePresentmentService(ledger).present_invoice_draft(
            TENANT_ONE, draft_id
        )
        self.assertEqual(statement.credited_amount, Decimal("150.00"))
        self.assertEqual(statement.amount_due, Decimal("0"))
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/invoice-drafts/{draft_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.InvoicePresentmentService.present_invoice_draft",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/invoice-drafts/{draft_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = InvoicePresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(InvoicePresentmentQueryError):
            empty.list_invoice_drafts(TENANT_ONE)
        with self.assertRaises(ExactDecimalError):
            remaining_amount_due(0.10, Decimal("1.00"))  # type: ignore[arg-type]
        service = InvoicePresentmentService(ledger)
        self.assertEqual(len(service.list_invoice_drafts(TENANT_ONE, cursor="").invoice_drafts), 1)
        self.assertEqual(len(service.list_invoice_drafts(TENANT_ONE, page_limit=1).invoice_drafts), 1)
        with self.assertRaises(InvoicePresentmentQueryError):
            service.list_invoice_drafts(TENANT_ONE, page_limit=True)
        with self.assertRaises(InvoicePresentmentQueryError):
            service.list_invoice_drafts(TENANT_ONE, page_limit=101)
        with self.assertRaises(InvoicePresentmentQueryError):
            service.list_invoice_drafts(TENANT_ONE, page_limit=1.5)
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        assessed_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        assessed = TaxAssessmentService(ledger).assess_tax(TENANT_ONE, assessed_id, 1)
        stored = ledger.get_tax_assessment(assessed.tax_assessment_id)
        assert stored is not None
        ledger.tax_assessments[stored.tax_assessment_id] = replace(
            stored, tax_amount=Decimal("99.00")
        )
        with self.assertRaises(InvoicePresentmentQueryError) as corrupt:
            service.present_invoice_draft(TENANT_ONE, assessed_id)
        self.assertEqual(corrupt.exception.rejection_reason_code, "request_invalid")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/invoice-drafts")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        put_collection_status, put_collection_body = invoke_http(app, "PUT", "/v1/invoice-drafts")
        self.assertEqual(put_collection_status, 422)
        bad_uuid_status, bad_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/invoice-drafts/" + ("-" * 36),
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bad_uuid_status, 422)
        self.assertEqual(bad_uuid_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.InvoicePresentmentService.list_invoice_drafts",
            side_effect=ExactDecimalError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/invoice-drafts",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")


def insert_statement_draft(ledger, tenant_reference: str, currency_code: str, drafted_total: Decimal):
    """Persist one invoice draft with a single explainable commercial line."""
    tenant = ledger.require_tenant(tenant_reference)
    draft_id = generate_record_id()
    line = StoredInvoiceDraftLine(
        invoice_draft_line_id=generate_record_id(),
        invoice_draft_id=draft_id,
        tenant_account_id=tenant.tenant_account_id,
        billing_account_id=generate_record_id(),
        billing_account_reference=f"{tenant_reference}:billing_account:presentment",
        meter_definition_id=generate_record_id(),
        meter_code="gen_ai_output_token",
        unit_code="token",
        rated_quantity=Decimal("100"),
        unit_price_amount=Decimal("1.00"),
        line_total_amount=drafted_total,
        line_number=1,
    )
    ledger.insert_invoice_draft(
        StoredInvoiceDraft(
            invoice_draft_id=draft_id,
            tenant_account_id=tenant.tenant_account_id,
            rating_run_id=generate_record_id(),
            usage_snapshot_hash="sha256:" + ("c" * 64),
            currency_code=currency_code,
            invoice_draft_status="draft",
            drafted_total_amount=drafted_total,
            recorded_at=datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            invoice_draft_lines=(line,),
        ),
        (line,),
    )
    return draft_id
