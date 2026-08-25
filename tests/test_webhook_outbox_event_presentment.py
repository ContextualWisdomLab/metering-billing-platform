"""Webhook-outbox-event HTTP presentment tests for metadata-only reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    WebhookDeliveryService,
    WebhookOutboxEventPresentmentService,
    create_http_app,
)
from metering_billing.contracts import validate_webhook_outbox_event_presentment
from metering_billing.errors import WebhookOutboxEventPresentmentQueryError
from metering_billing.usage_ledger import (
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
    generate_record_id,
)
from metering_billing.webhook_outbox import EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
from metering_billing.webhook_outbox_event_presentment import next_operator_action
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_webhook_outbox import _seed_tenants


ENQUEUED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ENQUEUED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
LEAKY_PAYLOAD = '{"secret_blob":"must-not-leak","card_pan":"4111111111111111"}'
HASH_MORNING = "sha256:" + ("a" * 64)
HASH_EVENING = "sha256:" + ("b" * 64)


def persist_outbox_event(
    ledger,
    tenant_reference,
    *,
    enqueued_at,
    delivery_status="pending",
    event_type_code=EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    payload_hash=HASH_MORNING,
    payload_json=LEAKY_PAYLOAD,
    source_id=None,
):
    """Persist one stored #24 webhook_outbox_event without sending a callback."""
    tenant = ledger.require_tenant(tenant_reference)
    return ledger.insert_webhook_outbox_event(
        StoredWebhookOutboxEvent(
            outbox_event_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            event_type_code=event_type_code,
            payload_hash=payload_hash,
            source_id=source_id or generate_record_id(),
            occurred_at=enqueued_at,
            delivery_status=delivery_status,
            payload_json=payload_json,
            enqueued_at=enqueued_at,
        )
    )


class WebhookOutboxEventPresentmentTests(unittest.TestCase):
    """Verify metadata GET, list paging, and fail-closed payload isolation."""

    def test_stored_pending_projects_run_deliveries_without_payload(self) -> None:
        """A pending stored outbox event shows run_deliveries and never leaks the body."""
        ledger = _seed_tenants()
        stored = persist_outbox_event(ledger, TENANT_ONE, enqueued_at=ENQUEUED_MORNING)
        first = WebhookOutboxEventPresentmentService(ledger).present_webhook_outbox_event(
            TENANT_ONE, stored.outbox_event_id
        )
        second = WebhookOutboxEventPresentmentService(ledger).present_webhook_outbox_event(
            TENANT_ONE, stored.outbox_event_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.outbox_event_id, stored.outbox_event_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.event_type_code, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
        self.assertEqual(first.source_id, stored.source_id)
        self.assertEqual(first.payload_hash, HASH_MORNING)
        self.assertEqual(first.delivery_status, "pending")
        self.assertEqual(first.enqueued_at, ENQUEUED_MORNING)
        self.assertEqual(first.occurred_at, ENQUEUED_MORNING)
        self.assertEqual(first.attempted_delivery_count, 0)
        self.assertEqual(first.next_operator_action, "run_deliveries")
        payload = first.as_contract_dict()
        self.assertEqual(validate_webhook_outbox_event_presentment(payload), ())
        self.assertNotIn("payload_json", payload)
        self.assertNotIn("secret_blob", str(payload))
        self.assertNotIn("4111111111111111", str(payload))
        self.assertNotIn("webhook_secret", payload)
        self.assertNotIn("webhook_secret_hash", payload)
        self.assertNotIn("signature", payload)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)

    def test_stored_delivered_projects_wait_and_attempt_count(self) -> None:
        """A delivered stored event waits and reports stored attempt count only."""
        ledger = _seed_tenants()
        stored = persist_outbox_event(
            ledger,
            TENANT_ONE,
            enqueued_at=ENQUEUED_MORNING,
            delivery_status="delivered",
            payload_hash=HASH_EVENING,
        )
        ledger.insert_webhook_delivery_attempt(
            StoredWebhookDeliveryAttempt(
                delivery_attempt_id=generate_record_id(),
                outbox_event_id=stored.outbox_event_id,
                webhook_subscription_id=generate_record_id(),
                attempt_number=1,
                http_status=200,
                delivered_at=ENQUEUED_EVENING,
                failure_reason_code=None,
                attempted_at=ENQUEUED_EVENING,
            )
        )
        presented = WebhookOutboxEventPresentmentService(ledger).present_webhook_outbox_event(
            TENANT_ONE, stored.outbox_event_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(presented.delivery_status, "delivered")
        self.assertEqual(presented.attempted_delivery_count, 1)
        self.assertNotIn("payload_json", payload)
        self.assertEqual(validate_webhook_outbox_event_presentment(payload), ())

    def test_http_get_item_and_paged_list_without_sending(self) -> None:
        """GET item and list page stored metadata and never deliver."""
        ledger = _seed_tenants()
        first = persist_outbox_event(ledger, TENANT_ONE, enqueued_at=ENQUEUED_MORNING)
        second = persist_outbox_event(
            ledger,
            TENANT_ONE,
            enqueued_at=ENQUEUED_EVENING,
            payload_hash=HASH_EVENING,
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-outbox-events/{first.outbox_event_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["outbox_event_id"], str(first.outbox_event_id))
        self.assertEqual(body["event_type_code"], EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
        self.assertEqual(body["delivery_status"], "pending")
        self.assertEqual(body["next_operator_action"], "run_deliveries")
        self.assertNotIn("payload_json", body)
        self.assertNotIn(LEAKY_PAYLOAD, str(body))
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-outbox-events/{first.outbox_event_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-outbox-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"webhook_outbox_events", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["webhook_outbox_events"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["webhook_outbox_events"][0]
        self.assertEqual(
            set(first_summary),
            {
                "outbox_event_id",
                "event_type_code",
                "delivery_status",
                "enqueued_at",
                "attempted_delivery_count",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-outbox-events",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["webhook_outbox_events"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["outbox_event_id"],
            second_body["webhook_outbox_events"][0]["outbox_event_id"],
        }
        self.assertEqual(listed_ids, {str(first.outbox_event_id), str(second.outbox_event_id)})
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-outbox-events",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["webhook_outbox_events"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_deliver_write_stays_and_refuses_card_data(self) -> None:
        """POST webhook-deliveries stays #24; PAN and secrets are refused."""
        ledger = _seed_tenants()
        persist_outbox_event(ledger, TENANT_ONE, enqueued_at=ENQUEUED_MORNING)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-deliveries",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-deliveries",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["webhook_delivery_outcome_code"], "accepted")
        self.assertEqual(accepted_body["attempted_delivery_count"], 0)
        stored = next(iter(ledger.webhook_outbox_events.values()))
        self.assertEqual(stored.delivery_status, "pending")
        self.assertEqual(WebhookDeliveryService(ledger).deliver_due_events(TENANT_ONE).attempted_delivery_count, 0)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no body."""
        ledger = _seed_tenants()
        stored = persist_outbox_event(ledger, TENANT_ONE, enqueued_at=ENQUEUED_MORNING)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-outbox-events/{stored.outbox_event_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-outbox-events/{stored.outbox_event_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "webhook_outbox_event_not_found")
        self.assertNotIn("payload_json", other_body)
        self.assertNotIn("event_type_code", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-outbox-events/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "webhook_outbox_event_not_found")
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError) as crossed:
            WebhookOutboxEventPresentmentService(ledger).present_webhook_outbox_event(
                TENANT_TWO, stored.outbox_event_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "webhook_outbox_event_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(delivery_status="pending"), "run_deliveries")
        self.assertEqual(next_operator_action(delivery_status="delivered"), "wait")
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            next_operator_action(delivery_status="posted")
        ledger = _seed_tenants()
        stored = persist_outbox_event(ledger, TENANT_ONE, enqueued_at=ENQUEUED_MORNING)
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-outbox-events",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-outbox-events",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/webhook-outbox-events/{stored.outbox_event_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app,
            "PUT",
            "/v1/webhook-outbox-events",
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.WebhookOutboxEventPresentmentService.list_webhook_outbox_events",
            side_effect=WebhookOutboxEventPresentmentQueryError("webhook_outbox_event_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-outbox-events",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "webhook_outbox_event_not_found")
        with mock.patch(
            "metering_billing.http_app.WebhookOutboxEventPresentmentService.list_webhook_outbox_events",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-outbox-events",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = WebhookOutboxEventPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            empty.list_webhook_outbox_events(TENANT_ONE)
        service = WebhookOutboxEventPresentmentService(ledger)
        listed = service.list_webhook_outbox_events(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.webhook_outbox_events), 1)
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.list_webhook_outbox_events(TENANT_ONE, page_limit=True)
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.list_webhook_outbox_events(TENANT_ONE, page_limit=101)
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.list_webhook_outbox_events(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.list_webhook_outbox_events(TENANT_ONE, page_limit="abc")
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.list_webhook_outbox_events(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/webhook-outbox-events")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_webhook_outbox_events(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.webhook_outbox_events), 1)
        empty_limit = service.list_webhook_outbox_events(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.webhook_outbox_events), 1)
        self.assertEqual(
            service.list_webhook_outbox_events(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.present_webhook_outbox_event(TENANT_ONE, uuid4())
        with self.assertRaises(WebhookOutboxEventPresentmentQueryError):
            service.present_webhook_outbox_event("", stored.outbox_event_id)
        self.assertEqual(listed.webhook_outbox_events[0].enqueued_at, ENQUEUED_MORNING)
        self.assertIsInstance(stored.outbox_event_id, UUID)
