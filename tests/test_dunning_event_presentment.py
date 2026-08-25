"""Dunning-event HTTP presentment tests for stored collection_dunning_event rows."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    CollectionCaseService,
    DunningEventPresentmentService,
    PaymentIntentService,
    PaymentSettlementService,
    create_http_app,
)
from metering_billing.contracts import validate_dunning_event_presentment
from metering_billing.dunning_event_presentment import next_operator_action
from metering_billing.errors import DunningEventPresentmentQueryError
from metering_billing.usage_ledger import StoredCollectionDunningEvent, generate_record_id
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


OCCURRED_MORNING = datetime(2026, 8, 17, 21, 5, tzinfo=UTC)
OCCURRED_EVENING = datetime(2026, 8, 17, 22, 5, tzinfo=UTC)


def persist_dunning_event(ledger, collection_case_id, *, occurred_at, dunning_notice_code):
    """Persist one stored #10 collection_dunning_event without sending mail."""
    tenant = ledger.require_tenant(TENANT_ONE)
    existing = ledger.list_collection_dunning_events(collection_case_id)
    return ledger.insert_collection_dunning_event(
        StoredCollectionDunningEvent(
            collection_dunning_event_id=generate_record_id(),
            collection_case_id=collection_case_id,
            tenant_account_id=tenant.tenant_account_id,
            dunning_event_number=len(existing) + 1,
            dunning_notice_code=dunning_notice_code,
            occurred_at=occurred_at,
        )
    )


class DunningEventPresentmentTests(unittest.TestCase):
    """Verify metadata GET, list paging, and fail-closed delivery isolation."""

    def test_stored_first_notice_projects_collect_without_delivery_fields(self) -> None:
        """A stored first_notice shows collect and never invents a send channel."""
        ledger, collection_case_id = open_known_morning_case()
        stored = persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_MORNING,
            dunning_notice_code="first_notice",
        )
        first = DunningEventPresentmentService(ledger).present_dunning_event(
            TENANT_ONE, stored.collection_dunning_event_id
        )
        second = DunningEventPresentmentService(ledger).present_dunning_event(
            TENANT_ONE, stored.collection_dunning_event_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.dunning_event_id, stored.collection_dunning_event_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.collection_case_id, collection_case_id)
        self.assertEqual(first.dunning_event_number, 1)
        self.assertEqual(first.dunning_notice_code, "first_notice")
        self.assertEqual(first.occurred_at, OCCURRED_MORNING)
        self.assertEqual(first.next_operator_action, "collect")
        payload = first.as_contract_dict()
        self.assertEqual(validate_dunning_event_presentment(payload), ())
        self.assertNotIn("recipient", payload)
        self.assertNotIn("email", payload)
        self.assertNotIn("phone", payload)
        self.assertNotIn("channel", payload)
        self.assertNotIn("provider_id", payload)
        self.assertNotIn("delivery_status", payload)
        self.assertNotIn("body", payload)
        self.assertNotIn("content", payload)
        self.assertNotIn("sent_at", payload)
        self.assertNotIn("scheduled_at", payload)
        self.assertNotIn("notice_amount", payload)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)

    def test_stored_overdue_notice_keeps_collect(self) -> None:
        """An overdue stored reminder still asks to collect, not to send mail."""
        ledger, collection_case_id = open_known_morning_case()
        persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_MORNING,
            dunning_notice_code="first_notice",
        )
        stored = persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_EVENING,
            dunning_notice_code="overdue_notice",
        )
        presented = DunningEventPresentmentService(ledger).present_dunning_event(
            TENANT_ONE, stored.collection_dunning_event_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(presented.next_operator_action, "collect")
        self.assertEqual(presented.dunning_notice_code, "overdue_notice")
        self.assertEqual(presented.dunning_event_number, 2)
        self.assertEqual(validate_dunning_event_presentment(payload), ())

    def test_notice_on_settled_case_waits(self) -> None:
        """A reminder on a settled case waits and does not invent another send."""
        ledger, collection_case_id = open_known_morning_case()
        stored = persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_MORNING,
            dunning_notice_code="first_notice",
        )
        intent = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        assert intent.payment_intent_id is not None
        PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, intent.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        presented = DunningEventPresentmentService(ledger).present_dunning_event(
            TENANT_ONE, stored.collection_dunning_event_id
        )
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(validate_dunning_event_presentment(presented.as_contract_dict()), ())

    def test_http_get_item_and_paged_list_without_delivery_engine(self) -> None:
        """GET item and list page stored reminders and never send."""
        ledger, collection_case_id = open_known_morning_case()
        times = iter((OCCURRED_MORNING, OCCURRED_EVENING))
        recorder = CollectionCaseService(ledger, clock=lambda: next(times))
        first = recorder.record_dunning_event(TENANT_ONE, collection_case_id, "first_notice")
        second = recorder.record_dunning_event(TENANT_ONE, collection_case_id, "overdue_notice")
        first_id = first.dunning_events[0].dunning_event_id
        second_id = second.dunning_events[1].dunning_event_id
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/dunning-events/{first_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["dunning_event_id"], str(first_id))
        self.assertEqual(body["collection_case_id"], str(collection_case_id))
        self.assertEqual(body["dunning_notice_code"], "first_notice")
        self.assertEqual(body["next_operator_action"], "collect")
        self.assertNotIn("recipient", body)
        self.assertNotIn("delivery_status", body)
        self.assertNotIn("body", body)
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/dunning-events/{first_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/dunning-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"dunning_events", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["dunning_events"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["dunning_events"][0]
        self.assertEqual(
            set(first_summary),
            {
                "dunning_event_id",
                "collection_case_id",
                "dunning_event_number",
                "dunning_notice_code",
                "occurred_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/dunning-events",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["dunning_events"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["dunning_event_id"],
            second_body["dunning_events"][0]["dunning_event_id"],
        }
        self.assertEqual(listed_ids, {str(first_id), str(second_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/dunning-events",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["dunning_events"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_record_write_stays_and_refuses_card_data(self) -> None:
        """POST dunning-events stays #10; PAN and secrets are refused."""
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{collection_case_id}/dunning-events",
            {
                "tenant_reference": TENANT_ONE,
                "dunning_notice_code": "first_notice",
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.collection_dunning_events), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{collection_case_id}/dunning-events",
            {"tenant_reference": TENANT_ONE, "dunning_notice_code": "first_notice"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["collection_case_outcome_code"], "accepted")
        self.assertEqual(accepted_body["collection_case_status"], "dunning")
        self.assertEqual(accepted_body["dunning_events"][0]["dunning_notice_code"], "first_notice")
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{collection_case_id}/dunning-events",
            {"tenant_reference": TENANT_ONE, "dunning_notice_code": "first_notice"},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["collection_case_outcome_code"], "duplicate_replay")
        self.assertEqual(len(ledger.collection_dunning_events), 1)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no PII."""
        ledger, collection_case_id = open_known_morning_case()
        stored = persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_MORNING,
            dunning_notice_code="first_notice",
        )
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/dunning-events/{stored.collection_dunning_event_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/dunning-events/{stored.collection_dunning_event_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "dunning_event_not_found")
        self.assertNotIn("dunning_notice_code", other_body)
        self.assertNotIn("recipient", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/dunning-events/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "dunning_event_not_found")
        with self.assertRaises(DunningEventPresentmentQueryError) as crossed:
            DunningEventPresentmentService(ledger).present_dunning_event(
                TENANT_TWO, stored.collection_dunning_event_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "dunning_event_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(collection_case_status="open"), "collect")
        self.assertEqual(next_operator_action(collection_case_status="dunning"), "collect")
        self.assertEqual(next_operator_action(collection_case_status="settled"), "wait")
        with self.assertRaises(DunningEventPresentmentQueryError):
            next_operator_action(collection_case_status="posted")
        ledger, collection_case_id = open_known_morning_case()
        stored = persist_dunning_event(
            ledger,
            collection_case_id,
            occurred_at=OCCURRED_MORNING,
            dunning_notice_code="first_notice",
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/dunning-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/dunning-events",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/dunning-events/{stored.collection_dunning_event_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app,
            "PUT",
            "/v1/dunning-events",
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.DunningEventPresentmentService.list_dunning_events",
            side_effect=DunningEventPresentmentQueryError("dunning_event_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/dunning-events",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "dunning_event_not_found")
        with mock.patch(
            "metering_billing.http_app.DunningEventPresentmentService.list_dunning_events",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/dunning-events",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = DunningEventPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(DunningEventPresentmentQueryError):
            empty.list_dunning_events(TENANT_ONE)
        service = DunningEventPresentmentService(ledger)
        listed = service.list_dunning_events(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.dunning_events), 1)
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.list_dunning_events(TENANT_ONE, page_limit=True)
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.list_dunning_events(TENANT_ONE, page_limit=101)
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.list_dunning_events(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.list_dunning_events(TENANT_ONE, page_limit="abc")
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.list_dunning_events(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/dunning-events")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_dunning_events(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.dunning_events), 1)
        empty_limit = service.list_dunning_events(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.dunning_events), 1)
        self.assertEqual(
            service.list_dunning_events(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.present_dunning_event(TENANT_ONE, uuid4())
        with self.assertRaises(DunningEventPresentmentQueryError):
            service.present_dunning_event("", stored.collection_dunning_event_id)
        self.assertEqual(listed.dunning_events[0].occurred_at, OCCURRED_MORNING)
        with mock.patch(
            "metering_billing.http_app.CollectionCaseService.record_dunning_event",
            side_effect=ValueError("closed"),
        ):
            write_value_status, write_value_body = invoke_http(
                create_http_app(ledger),
                "POST",
                f"/v1/collection-cases/{collection_case_id}/dunning-events",
                {"tenant_reference": TENANT_ONE, "dunning_notice_code": "first_notice"},
            )
        self.assertEqual(write_value_status, 422)
        self.assertEqual(write_value_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(
            service.present_dunning_event(
                TENANT_ONE, stored.collection_dunning_event_id
            ).dunning_event_id,
            stored.collection_dunning_event_id,
        )
        self.assertIsInstance(stored.collection_dunning_event_id, UUID)
        with mock.patch.object(ledger, "get_collection_case", return_value=None):
            with self.assertRaises(DunningEventPresentmentQueryError) as missing_case:
                service.present_dunning_event(TENANT_ONE, stored.collection_dunning_event_id)
        self.assertEqual(missing_case.exception.rejection_reason_code, "dunning_event_not_found")
        parent = ledger.get_collection_case(collection_case_id)
        assert parent is not None
        with mock.patch.object(
            ledger,
            "get_collection_case",
            return_value=replace(parent, tenant_account_id=uuid4()),
        ):
            with self.assertRaises(DunningEventPresentmentQueryError) as crossed_case:
                service.present_dunning_event(TENANT_ONE, stored.collection_dunning_event_id)
        self.assertEqual(crossed_case.exception.rejection_reason_code, "dunning_event_not_found")
