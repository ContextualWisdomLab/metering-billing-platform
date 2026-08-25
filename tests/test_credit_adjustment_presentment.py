"""Credit-adjustment HTTP presentment tests for tenant-scoped credit reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    CreditAdjustmentPresentmentService,
    CreditAdjustmentService,
    create_http_app,
)
from metering_billing.contracts import validate_credit_adjustment_presentment
from metering_billing.errors import CreditAdjustmentPresentmentQueryError
from metering_billing.credit_adjustment_presentment import next_operator_action
from metering_billing.webhook_outbox import EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED
from test_collection_case import draft_known_morning
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_tax_assessment import insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


def record_known_morning_credit(credit_amount=None):
    """Record one known-morning credit against the stored invoice draft."""
    ledger, collection_case_id = open_known_morning_case()
    invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
    amount = KNOWN_MORNING_TOTAL if credit_amount is None else credit_amount
    credit = CreditAdjustmentService(ledger).record_credit_adjustment(
        TENANT_ONE, invoice_draft_id, amount, "goodwill"
    )
    assert credit.credit_adjustment_id is not None
    return ledger, credit.credit_adjustment_id, invoice_draft_id


class CreditAdjustmentPresentmentTests(unittest.TestCase):
    """Verify recorded-credit GET, list envelope, and fail-closed isolation."""

    def test_full_morning_credit_projects_amount_and_wait(self) -> None:
        """A full known-morning credit shows exact amounts and wait."""
        ledger, credit_adjustment_id, invoice_draft_id = record_known_morning_credit()
        first = CreditAdjustmentPresentmentService(ledger).present_credit_adjustment(
            TENANT_ONE, credit_adjustment_id
        )
        second = CreditAdjustmentPresentmentService(ledger).present_credit_adjustment(
            TENANT_ONE, credit_adjustment_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.credit_adjustment_id, credit_adjustment_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.invoice_draft_id, invoice_draft_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.tax_exclusive_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.tax_amount, Decimal("0"))
        self.assertEqual(first.credit_adjustment_status, "recorded")
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_credit_adjustment_presentment(payload), ())
        self.assertIsInstance(payload["credit_amount"], str)
        self.assertNotIsInstance(payload["credit_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("credit_adjustment_outcome_code", payload)
        self.assertNotIn("credit_tax_amount", payload)
        self.assertNotIn("credit_exclusive_amount", payload)
        credit_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED
        ]
        self.assertEqual(len(credit_events), 1)
        self.assertEqual(len(ledger.journal_proposals), 1)
        journal = next(iter(ledger.journal_proposals.values()))
        self.assertEqual(journal.proposal_status, "validated")
        roles = {line.account_role_code for line in journal.proposal_lines}
        self.assertEqual(roles, {"usage_revenue", "accounts_receivable"})

    def test_http_get_returns_presentment_and_list_envelope(self) -> None:
        """GET is 200 presentment; list uses {credit_adjustments, next_cursor}."""
        first_ledger, first_draft_id = draft_known_morning()
        second_draft = insert_commercial_draft(first_ledger, TENANT_ONE, "USD", Decimal("20.00"))
        CollectionCaseService(first_ledger).open_collection_case(TENANT_ONE, first_draft_id)
        CollectionCaseService(first_ledger).open_collection_case(TENANT_ONE, second_draft)
        first = CreditAdjustmentService(
            first_ledger, clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ).record_credit_adjustment(TENANT_ONE, first_draft_id, KNOWN_MORNING_TOTAL, "goodwill")
        second = CreditAdjustmentService(
            first_ledger, clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
        ).record_credit_adjustment(TENANT_ONE, second_draft, Decimal("20.00"), "billing_error")
        assert first.credit_adjustment_id is not None
        assert second.credit_adjustment_id is not None
        app = create_http_app(first_ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{first.credit_adjustment_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["credit_adjustment_id"], str(first.credit_adjustment_id))
        self.assertEqual(body["invoice_draft_id"], str(first_draft_id))
        self.assertEqual(body["credit_amount"], str(KNOWN_MORNING_TOTAL))
        self.assertEqual(body["tax_exclusive_amount"], str(KNOWN_MORNING_TOTAL))
        self.assertEqual(body["tax_amount"], "0")
        self.assertEqual(body["credit_adjustment_status"], "recorded")
        self.assertEqual(body["next_operator_action"], "wait")
        self.assertNotIn("proposal_id", body)
        self.assertEqual(validate_credit_adjustment_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{first.credit_adjustment_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/credit-adjustments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"credit_adjustments", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["credit_adjustments"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["credit_adjustments"][0]
        self.assertEqual(
            set(first_summary),
            {
                "credit_adjustment_id",
                "credit_amount",
                "currency_code",
                "credit_adjustment_status",
                "recorded_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/credit-adjustments",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["credit_adjustments"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["credit_adjustment_id"],
            second_body["credit_adjustments"][0]["credit_adjustment_id"],
        }
        self.assertEqual(
            listed_ids,
            {str(first.credit_adjustment_id), str(second.credit_adjustment_id)},
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/credit-adjustments",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["credit_adjustments"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_write_and_refuses_card_data(self) -> None:
        """POST stays the #17 record; PAN and secrets are refused."""
        ledger, collection_case_id = open_known_morning_case()
        invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": str(KNOWN_MORNING_TOTAL),
                "credit_reason_code": "goodwill",
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.credit_adjustments), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            "/v1/credit-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(invoice_draft_id),
                "credit_amount": str(KNOWN_MORNING_TOTAL),
                "credit_reason_code": "goodwill",
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["credit_adjustment_outcome_code"], "accepted")
        self.assertEqual(accepted_body["proposal_status"], "validated")

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no credit."""
        ledger, credit_adjustment_id, _draft_id = record_known_morning_credit()
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/credit-adjustments/{credit_adjustment_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{credit_adjustment_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "credit_adjustment_not_found")
        self.assertNotIn("credit_amount", other_body)
        self.assertNotIn("invoice_draft_id", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/credit-adjustments/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "credit_adjustment_not_found")
        with self.assertRaises(CreditAdjustmentPresentmentQueryError) as crossed:
            CreditAdjustmentPresentmentService(ledger).present_credit_adjustment(
                TENANT_TWO, credit_adjustment_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "credit_adjustment_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and wait."""
        self.assertEqual(next_operator_action(), "wait")
        ledger, credit_adjustment_id, _draft_id = record_known_morning_credit()
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/credit-adjustments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/credit-adjustments",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/credit-adjustments/{credit_adjustment_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CreditAdjustmentPresentmentService.present_credit_adjustment",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/credit-adjustments/{credit_adjustment_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = CreditAdjustmentPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            empty.list_credit_adjustments(TENANT_ONE)
        service = CreditAdjustmentPresentmentService(ledger)
        self.assertEqual(
            len(service.list_credit_adjustments(TENANT_ONE, cursor="").credit_adjustments), 1
        )
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.list_credit_adjustments(TENANT_ONE, page_limit=True)
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.list_credit_adjustments(TENANT_ONE, page_limit=101)
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.list_credit_adjustments(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.list_credit_adjustments(TENANT_ONE, page_limit="abc")
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.list_credit_adjustments(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/credit-adjustments")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.CreditAdjustmentPresentmentService.list_credit_adjustments",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/credit-adjustments",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_credit_adjustments(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.credit_adjustments), 1)
        empty_limit = service.list_credit_adjustments(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.credit_adjustments), 1)
        self.assertEqual(service.list_credit_adjustments(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.present_credit_adjustment(TENANT_ONE, uuid4())
        with self.assertRaises(CreditAdjustmentPresentmentQueryError):
            service.present_credit_adjustment("", credit_adjustment_id)
