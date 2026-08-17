"""Usage-event HTTP presentment tests for tenant-scoped usage reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import uuid4

from metering_billing import (
    UsageEventPresentmentService,
    UsageIngestionService,
    create_http_app,
)
from metering_billing.contracts import validate_usage_event_presentment
from metering_billing.errors import UsageEventPresentmentQueryError
from metering_billing.usage_event_presentment import next_operator_action
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, make_event, seed_ledger


def ingest_named_event(ledger, source_event_key, quantity="1810", clock=None):
    """Ingest one #5 usage event and return the stored identifier."""
    service = (
        UsageIngestionService(ledger, clock=clock)
        if clock is not None
        else UsageIngestionService(ledger)
    )
    receipt = service.ingest_usage_event(
        make_event(
            event_id=str(uuid4()),
            source_event_key=source_event_key,
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": quantity,
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
    )
    assert receipt.usage_event_id is not None
    return receipt


class UsageEventPresentmentTests(unittest.TestCase):
    """Verify stored-usage GET, list envelope, and fail-closed isolation."""

    def test_stored_event_projects_quantity_and_rate_window(self) -> None:
        """A stored morning event shows exact quantity and rate_window."""
        ledger = seed_ledger()
        ingested = ingest_named_event(ledger, "workflow_381:step_04:attempt_01")
        first = UsageEventPresentmentService(ledger).present_usage_event(
            TENANT_ONE, ingested.usage_event_id
        )
        second = UsageEventPresentmentService(ledger).present_usage_event(
            TENANT_ONE, ingested.usage_event_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.usage_event_id, ingested.usage_event_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.source_event_key, "workflow_381:step_04:attempt_01")
        self.assertEqual(first.measurements[0].meter_code, "gen_ai_output_token")
        self.assertEqual(str(first.measurements[0].quantity), "1810")
        self.assertEqual(first.measurements[0].unit_code, "token")
        self.assertEqual(first.next_operator_action, "rate_window")
        payload = first.as_contract_dict()
        self.assertEqual(validate_usage_event_presentment(payload), ())
        self.assertIsInstance(payload["measurements"][0]["quantity"], str)
        self.assertNotIsInstance(payload["measurements"][0]["quantity"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("ingestion_outcome_code", payload)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("usage_event_status", payload)

    def test_http_get_returns_presentment_and_list_envelope(self) -> None:
        """GET is 200 presentment; list uses {usage_events, next_cursor}."""
        ledger = seed_ledger()
        first = ingest_named_event(
            ledger,
            "workflow_381:step_04:attempt_01",
            clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
        )
        second = ingest_named_event(
            ledger,
            "workflow_381:step_05:attempt_01",
            quantity="42.5",
            clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/usage-events/{first.usage_event_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["usage_event_id"], str(first.usage_event_id))
        self.assertEqual(body["source_event_key"], "workflow_381:step_04:attempt_01")
        self.assertEqual(body["measurements"][0]["quantity"], "1810")
        self.assertEqual(body["next_operator_action"], "rate_window")
        self.assertNotIn("ingestion_outcome_code", body)
        self.assertEqual(validate_usage_event_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/usage-events/{first.usage_event_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/usage-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"usage_events", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["usage_events"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["usage_events"][0]
        self.assertEqual(
            set(first_summary),
            {
                "usage_event_id",
                "source_event_key",
                "recorded_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/usage-events",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["usage_events"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["usage_event_id"],
            second_body["usage_events"][0]["usage_event_id"],
        }
        self.assertEqual(listed_ids, {str(first.usage_event_id), str(second.usage_event_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/usage-events",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["usage_events"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_ingest_and_refuses_card_data(self) -> None:
        """POST stays the #5 ingest; PAN and secrets are refused."""
        ledger = seed_ledger()
        app = create_http_app(ledger)
        event = make_event(event_id=str(uuid4()), source_event_key="workflow_400:step_01:attempt_01")
        status, body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {**event, "card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.usage_events), 0)
        batch_status, batch_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {
                "tenant_reference": TENANT_ONE,
                "events": [{**event, "cvc": "123"}],
            },
        )
        self.assertEqual(batch_status, 422)
        self.assertEqual(batch_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(app, "POST", "/v1/usage-events", event)
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["ingestion_outcome_code"], "accepted")
        self.assertIn("usage_event_id", accepted_body)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no usage."""
        ledger = seed_ledger()
        ingested = ingest_named_event(ledger, "workflow_381:step_04:attempt_01")
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/usage-events/{ingested.usage_event_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/usage-events/{ingested.usage_event_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "usage_event_not_found")
        self.assertNotIn("quantity", other_body)
        self.assertNotIn("source_event_key", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/usage-events/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "usage_event_not_found")
        with self.assertRaises(UsageEventPresentmentQueryError) as crossed:
            UsageEventPresentmentService(ledger).present_usage_event(
                TENANT_TWO, ingested.usage_event_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "usage_event_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and rate_window."""
        self.assertEqual(next_operator_action(), "rate_window")
        ledger = seed_ledger()
        ingested = ingest_named_event(ledger, "workflow_381:step_04:attempt_01")
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/usage-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/usage-events",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/usage-events/{ingested.usage_event_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.UsageEventPresentmentService.present_usage_event",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/usage-events/{ingested.usage_event_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = UsageEventPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(UsageEventPresentmentQueryError):
            empty.list_usage_events(TENANT_ONE)
        service = UsageEventPresentmentService(ledger)
        listed = service.list_usage_events(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.usage_events), 1)
        self.assertEqual(ledger.get_usage_event(ingested.usage_event_id).usage_event_id, ingested.usage_event_id)
        self.assertEqual(len(ledger.list_usage_events(ledger.require_tenant(TENANT_ONE).tenant_account_id)), 1)
        self.assertEqual(len(ledger.list_usage_events()), 1)
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.list_usage_events(TENANT_ONE, page_limit=True)
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.list_usage_events(TENANT_ONE, page_limit=101)
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.list_usage_events(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.list_usage_events(TENANT_ONE, page_limit="abc")
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.list_usage_events(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/usage-events")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        with mock.patch(
            "metering_billing.http_app.UsageEventPresentmentService.list_usage_events",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/usage-events",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_usage_events(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.usage_events), 1)
        empty_limit = service.list_usage_events(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.usage_events), 1)
        self.assertEqual(service.list_usage_events(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.present_usage_event(TENANT_ONE, uuid4())
        with self.assertRaises(UsageEventPresentmentQueryError):
            service.present_usage_event("", ingested.usage_event_id)
        self.assertIsNone(ledger.get_usage_event(uuid4()))
