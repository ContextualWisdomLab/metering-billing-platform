"""Collection-case presentment tests for tenant-scoped collection reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCasePresentmentService,
    CollectionCaseService,
    CreditAdjustmentService,
    PaymentIntentService,
    PaymentSettlementService,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
)
from metering_billing.contracts import validate_collection_case_presentment
from metering_billing.errors import CollectionCasePresentmentQueryError
from metering_billing.collection_case_presentment import next_operator_action
from test_http_app import invoke_http
from test_invoice_presentment import insert_statement_draft
from test_payment_intent import open_known_morning_case
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


class CollectionCasePresentmentTests(unittest.TestCase):
    """Verify projected collection cases, HTTP GET, and fail-closed isolation."""

    def test_open_morning_case_projects_outstanding_and_collect_action(self) -> None:
        """An open known-morning case shows exact outstanding and collect."""
        ledger, collection_case_id = open_known_morning_case()
        first = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection_case_id
        )
        second = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection_case_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.collection_case_id, collection_case_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.collection_outstanding, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.collection_case_status, "open")
        self.assertEqual(first.next_operator_action, "collect")
        self.assertEqual(first.last_dunning_notice_code, None)
        self.assertEqual(first.next_dunning_notice_code, "first_notice")
        self.assertEqual(first.dunning_events, ())
        payload = first.as_contract_dict()
        self.assertEqual(validate_collection_case_presentment(payload), ())
        self.assertIsInstance(payload["collection_outstanding"], str)
        self.assertNotIsInstance(payload["collection_outstanding"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("proposal_status", payload)

    def test_dunning_projects_last_notice_and_next_overdue(self) -> None:
        """Existing dunning rows project last notice and the next overdue step."""
        ledger, collection_case_id = open_known_morning_case()
        CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection_case_id, "first_notice"
        )
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "dunning")
        self.assertEqual(presented.last_dunning_notice_code, "first_notice")
        self.assertEqual(presented.next_dunning_notice_code, "overdue_notice")
        self.assertEqual(presented.next_operator_action, "collect")
        self.assertEqual(len(presented.dunning_events), 1)
        self.assertEqual(presented.dunning_events[0].dunning_notice_code, "first_notice")
        CollectionCaseService(ledger).record_dunning_event(
            TENANT_ONE, collection_case_id, "overdue_notice"
        )
        overdue = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection_case_id
        )
        self.assertEqual(overdue.last_dunning_notice_code, "overdue_notice")
        self.assertIsNone(overdue.next_dunning_notice_code)
        self.assertEqual(overdue.next_operator_action, "collect")
        self.assertEqual(validate_collection_case_presentment(overdue.as_contract_dict()), ())

    def test_settled_case_waits_and_does_not_offer_another_notice(self) -> None:
        """A settled case shows zero outstanding and wait."""
        ledger, collection_case_id = open_known_morning_case()
        intent = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        assert intent.payment_intent_id is not None
        PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "settled")
        self.assertEqual(presented.collection_outstanding, Decimal("0"))
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertIsNone(presented.next_dunning_notice_code)
        self.assertEqual(validate_collection_case_presentment(presented.as_contract_dict()), ())

    def test_open_case_after_credit_still_uses_store_status_vocabulary(self) -> None:
        """Credits do not invent a new case status; open stays open."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        draft_id = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft_id, 1)
        opened = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
        CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, draft_id, Decimal("11.00"), "goodwill"
        )
        other_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, other_draft, Decimal("1.00"), "goodwill"
        )
        assert opened.collection_case_id is not None
        presented = CollectionCasePresentmentService(ledger).present_collection_case(
            TENANT_ONE, opened.collection_case_id
        )
        self.assertEqual(presented.collection_case_status, "open")
        self.assertEqual(presented.collection_outstanding, Decimal("99.00"))
        self.assertEqual(presented.next_operator_action, "credit")
        self.assertIn(presented.collection_case_status, {"open", "dunning", "settled"})
        self.assertNotIn(presented.collection_case_status, {"paid", "written_off", "posted"})

    def test_http_get_returns_case_and_list_envelope(self) -> None:
        """Same-tenant GET is 200; list uses {collection_cases, next_cursor} only."""
        ledger = seed_rated_ledger()
        first_draft = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        second_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        first = CollectionCaseService(
            ledger, clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ).open_collection_case(TENANT_ONE, first_draft)
        second = CollectionCaseService(
            ledger, clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
        ).open_collection_case(TENANT_ONE, second_draft)
        assert first.collection_case_id is not None
        assert second.collection_case_id is not None
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{first.collection_case_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["collection_case_id"], str(first.collection_case_id))
        self.assertEqual(body["collection_outstanding"], "100.00")
        self.assertEqual(body["collection_case_status"], "open")
        self.assertEqual(body["next_operator_action"], "collect")
        self.assertEqual(validate_collection_case_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{first.collection_case_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        post_status, post_body = invoke_http(
            app,
            "POST",
            "/v1/collection-cases",
            {"tenant_reference": TENANT_ONE, "invoice_draft_id": str(first_draft)},
        )
        self.assertEqual(post_status, 200)
        self.assertEqual(post_body["collection_case_outcome_code"], "duplicate_replay")

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"collection_cases", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["collection_cases"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["collection_cases"][0]
        self.assertEqual(
            set(first_summary),
            {
                "collection_case_id",
                "collection_outstanding",
                "currency_code",
                "collection_case_status",
                "opened_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["collection_cases"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["collection_case_id"],
            second_body["collection_cases"][0]["collection_case_id"],
        }
        self.assertEqual(listed_ids, {str(first.collection_case_id), str(second.collection_case_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["collection_cases"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no case."""
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/collection-cases/{collection_case_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{collection_case_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "collection_case_not_found")
        self.assertNotIn("collection_outstanding", other_body)
        self.assertNotIn("dunning_events", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "collection_case_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            f"/v1/collection-cases/{collection_case_id}",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        with self.assertRaises(CollectionCasePresentmentQueryError) as missing_tenant:
            CollectionCasePresentmentService(ledger).present_collection_case("", collection_case_id)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(CollectionCasePresentmentQueryError) as crossed:
            CollectionCasePresentmentService(ledger).present_collection_case(
                TENANT_TWO, collection_case_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "collection_case_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action("settled", Decimal("0"), Decimal("0")), "wait")
        self.assertEqual(next_operator_action("open", Decimal("0"), Decimal("0")), "wait")
        self.assertEqual(next_operator_action("open", Decimal("10.00"), Decimal("0")), "collect")
        self.assertEqual(next_operator_action("open", Decimal("10.00"), Decimal("5.00")), "credit")
        self.assertEqual(next_operator_action("dunning", Decimal("10.00"), Decimal("5.00")), "collect")
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/collection-cases/{collection_case_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CollectionCasePresentmentService.present_collection_case",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/collection-cases/{collection_case_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = CollectionCasePresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            empty.list_collection_cases(TENANT_ONE)
        service = CollectionCasePresentmentService(ledger)
        self.assertEqual(len(service.list_collection_cases(TENANT_ONE, cursor="").collection_cases), 1)
        self.assertEqual(len(service.list_collection_cases(TENANT_ONE, page_limit=1).collection_cases), 1)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.list_collection_cases(TENANT_ONE, page_limit=True)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.list_collection_cases(TENANT_ONE, page_limit=101)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.list_collection_cases(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.list_collection_cases(TENANT_ONE, page_limit="abc")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/collection-cases")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        put_collection_status, put_collection_body = invoke_http(app, "PUT", "/v1/collection-cases")
        self.assertEqual(put_collection_status, 422)
        bad_uuid_status, bad_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/collection-cases/" + ("-" * 36),
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bad_uuid_status, 422)
        self.assertEqual(bad_uuid_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.CollectionCasePresentmentService.list_collection_cases",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/collection-cases",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_collection_cases(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.collection_cases), 1)
        empty_limit = service.list_collection_cases(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.collection_cases), 1)
        self.assertEqual(service.list_collection_cases(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.list_collection_cases(TENANT_ONE, page_limit="0")
        with self.assertRaises(CollectionCasePresentmentQueryError):
            service.present_collection_case(TENANT_ONE, uuid4())
