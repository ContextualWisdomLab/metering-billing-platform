"""Webhook-delivery HTTP presentment tests for tenant-scoped attempt reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import uuid4

from metering_billing import (
    WebhookDeliveryPresentmentService,
    WebhookSubscriptionService,
    create_http_app,
)
from metering_billing.contracts import validate_webhook_delivery_presentment
from metering_billing.errors import WebhookDeliveryPresentmentQueryError
from metering_billing.usage_ledger import (
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
    generate_record_id,
)
from metering_billing.webhook_delivery_presentment import next_operator_action
from metering_billing.webhook_outbox import (
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    WebhookDeliveryService,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_webhook_outbox import HTTPS_CALLBACK, _seed_tenants


ATTEMPTED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ATTEMPTED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
EVENT_SET = (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,)


def persist_delivery_attempt(
    ledger,
    tenant_reference,
    *,
    attempted_at,
    delivered_at=None,
    http_status=None,
    failure_reason_code=None,
    attempt_number=1,
    source_id=None,
    payload_suffix="a",
    callback_url=None,
    payload_json='{"must_not_leak":true}',
):
    """Persist one stored #24 attempt without posting a callback."""
    tenant = ledger.require_tenant(tenant_reference)
    registered = WebhookSubscriptionService(ledger).register_subscription(
        tenant_reference,
        callback_url or HTTPS_CALLBACK,
        EVENT_SET,
    )
    outbox = ledger.insert_webhook_outbox_event(
        StoredWebhookOutboxEvent(
            outbox_event_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            event_type_code=EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            payload_hash="sha256:" + (payload_suffix * 64),
            source_id=source_id or generate_record_id(),
            occurred_at=attempted_at,
            delivery_status="pending",
            payload_json=payload_json,
            enqueued_at=attempted_at,
        )
    )
    stored = ledger.insert_webhook_delivery_attempt(
        StoredWebhookDeliveryAttempt(
            delivery_attempt_id=generate_record_id(),
            outbox_event_id=outbox.outbox_event_id,
            webhook_subscription_id=registered.webhook_subscription_id,
            attempt_number=attempt_number,
            http_status=http_status,
            delivered_at=delivered_at,
            failure_reason_code=failure_reason_code,
            attempted_at=attempted_at,
        )
    )
    return registered, outbox, stored


class WebhookDeliveryPresentmentTests(unittest.TestCase):
    """Verify stored-attempt GET, list envelope, and fail-closed isolation."""

    def test_stored_success_projects_wait_without_secret_or_body(self) -> None:
        """A stored success attempt shows wait and never leaks secret or body."""
        ledger = _seed_tenants()
        registered, outbox, stored = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_MORNING,
            delivered_at=ATTEMPTED_MORNING,
            http_status=200,
            payload_json='{"webhook_secret":"cwlwhsec_must_not_leak"}',
        )
        first = WebhookDeliveryPresentmentService(ledger).present_webhook_delivery(
            TENANT_ONE, stored.delivery_attempt_id
        )
        second = WebhookDeliveryPresentmentService(ledger).present_webhook_delivery(
            TENANT_ONE, stored.delivery_attempt_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.delivery_attempt_id, stored.delivery_attempt_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.webhook_subscription_id, registered.webhook_subscription_id)
        self.assertEqual(first.outbox_event_id, outbox.outbox_event_id)
        self.assertEqual(first.event_type_code, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
        self.assertEqual(first.source_id, outbox.source_id)
        self.assertEqual(first.attempt_number, 1)
        self.assertEqual(first.http_status, 200)
        self.assertEqual(first.delivered_at, ATTEMPTED_MORNING)
        self.assertIsNone(first.failure_reason_code)
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_webhook_delivery_presentment(payload), ())
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("delivery_status", payload)
        self.assertNotIn("webhook_delivery_status", payload)
        self.assertNotIn("webhook_secret", payload)
        self.assertNotIn("webhook_secret_hash", payload)
        self.assertNotIn("payload_json", payload)
        self.assertNotIn("payload_hash", payload)
        self.assertNotIn("card_pan", payload)
        serialized = str(payload)
        self.assertNotIn("cwlwhsec_must_not_leak", serialized)
        self.assertNotIn(registered.webhook_secret or "", serialized)

    def test_stored_failure_projects_run_deliveries(self) -> None:
        """A stored failure attempt keeps the HTTP status and asks to run deliveries."""
        ledger = _seed_tenants()
        _registered, _outbox, stored = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_EVENING,
            http_status=500,
            failure_reason_code="webhook_http_error",
            payload_suffix="b",
        )
        presented = WebhookDeliveryPresentmentService(ledger).present_webhook_delivery(
            TENANT_ONE, stored.delivery_attempt_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(presented.next_operator_action, "run_deliveries")
        self.assertEqual(presented.http_status, 500)
        self.assertEqual(presented.failure_reason_code, "webhook_http_error")
        self.assertIsNone(presented.delivered_at)
        self.assertNotIn("delivered_at", payload)
        self.assertNotIn("delivery_status", payload)
        self.assertEqual(validate_webhook_delivery_presentment(payload), ())

    def test_http_get_item_and_list_envelope_without_resend(self) -> None:
        """GET item and list use stored attempts; GET never reruns deliveries."""
        ledger = _seed_tenants()
        _first_sub, _first_outbox, first = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_MORNING,
            delivered_at=ATTEMPTED_MORNING,
            http_status=200,
        )
        _second_sub, _second_outbox, second = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_EVENING,
            http_status=500,
            failure_reason_code="webhook_http_error",
            payload_suffix="b",
        )
        attempt_count = len(ledger.webhook_delivery_attempts)
        app = create_http_app(ledger)
        with mock.patch.object(
            WebhookDeliveryService, "deliver_due_events", autospec=True
        ) as deliver:
            status, body = invoke_http(
                app,
                "GET",
                f"/v1/webhook-deliveries/{first.delivery_attempt_id}",
                query={"tenant_reference": TENANT_ONE},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["delivery_attempt_id"], str(first.delivery_attempt_id))
            self.assertEqual(body["event_type_code"], EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
            self.assertEqual(body["next_operator_action"], "wait")
            self.assertEqual(body["http_status"], 200)
            self.assertNotIn("webhook_secret", body)
            self.assertNotIn("payload_json", body)
            self.assertNotIn("delivery_status", body)
            replay_status, replay_body = invoke_http(
                app,
                "GET",
                f"/v1/webhook-deliveries/{first.delivery_attempt_id}",
                headers={"X-CWL-Tenant-Reference": TENANT_ONE},
            )
            self.assertEqual(replay_status, 200)
            self.assertEqual(replay_body, body)
            list_status, list_body = invoke_http(
                app,
                "GET",
                "/v1/webhook-deliveries",
                query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
            )
            deliver.assert_not_called()
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"webhook_deliveries", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["webhook_deliveries"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["webhook_deliveries"][0]
        self.assertEqual(
            set(first_summary),
            {
                "delivery_attempt_id",
                "webhook_subscription_id",
                "event_type_code",
                "attempt_number",
                "attempted_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-deliveries",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["webhook_deliveries"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["delivery_attempt_id"],
            second_body["webhook_deliveries"][0]["delivery_attempt_id"],
        }
        self.assertEqual(
            listed_ids,
            {str(first.delivery_attempt_id), str(second.delivery_attempt_id)},
        )
        self.assertEqual(len(ledger.webhook_delivery_attempts), attempt_count)
        ledger.register_tenant(TENANT_TWO) if TENANT_TWO not in ledger.tenant_accounts else None
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-deliveries",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["webhook_deliveries"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_run_and_refuses_card_data(self) -> None:
        """POST stays the #24 deliver run; PAN and secrets are refused."""
        ledger = _seed_tenants()
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/webhook-deliveries",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.webhook_delivery_attempts), 0)
        accepted_status, accepted_body = invoke_http(
            app, "POST", "/v1/webhook-deliveries", {"tenant_reference": TENANT_ONE}
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["webhook_delivery_outcome_code"], "accepted")
        self.assertIn("delivered_event_count", accepted_body)
        self.assertEqual(accepted_body["delivered_event_count"], 0)
        self.assertNotIn("delivery_attempt_id", accepted_body)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no body."""
        ledger = _seed_tenants()
        _registered, _outbox, stored = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_MORNING,
            delivered_at=ATTEMPTED_MORNING,
            http_status=200,
        )
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-deliveries/{stored.delivery_attempt_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-deliveries/{stored.delivery_attempt_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "webhook_delivery_not_found")
        self.assertNotIn("http_status", other_body)
        self.assertNotIn("payload_json", other_body)
        self.assertNotIn("webhook_secret", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-deliveries/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "webhook_delivery_not_found")
        with self.assertRaises(WebhookDeliveryPresentmentQueryError) as crossed:
            WebhookDeliveryPresentmentService(ledger).present_webhook_delivery(
                TENANT_TWO, stored.delivery_attempt_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "webhook_delivery_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(delivered_at=ATTEMPTED_MORNING), "wait")
        self.assertEqual(next_operator_action(delivered_at=None), "run_deliveries")
        ledger = _seed_tenants()
        _registered, _outbox, stored = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=ATTEMPTED_MORNING,
            delivered_at=ATTEMPTED_MORNING,
            http_status=200,
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-deliveries",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-deliveries",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/webhook-deliveries/{stored.delivery_attempt_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app, "PUT", "/v1/webhook-deliveries"
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.WebhookDeliveryPresentmentService.list_webhook_deliveries",
            side_effect=WebhookDeliveryPresentmentQueryError("webhook_delivery_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-deliveries",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "webhook_delivery_not_found")
        with mock.patch(
            "metering_billing.http_app.WebhookDeliveryPresentmentService.list_webhook_deliveries",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-deliveries",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = WebhookDeliveryPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            empty.list_webhook_deliveries(TENANT_ONE)
        service = WebhookDeliveryPresentmentService(ledger)
        listed = service.list_webhook_deliveries(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.webhook_deliveries), 1)
        tenant_one = ledger.require_tenant(TENANT_ONE)
        self.assertEqual(
            len(ledger.list_webhook_delivery_attempts_for_tenant(tenant_one.tenant_account_id)),
            1,
        )
        self.assertIsNone(ledger.get_webhook_delivery_attempt(uuid4()))
        self.assertIsNone(ledger.get_webhook_outbox_event(uuid4()))
        orphan = ledger.insert_webhook_delivery_attempt(
            StoredWebhookDeliveryAttempt(
                delivery_attempt_id=generate_record_id(),
                outbox_event_id=generate_record_id(),
                webhook_subscription_id=stored.webhook_subscription_id,
                attempt_number=2,
                http_status=None,
                delivered_at=None,
                failure_reason_code="webhook_http_error",
                attempted_at=ATTEMPTED_EVENING,
            )
        )
        self.assertEqual(
            len(ledger.list_webhook_delivery_attempts_for_tenant(tenant_one.tenant_account_id)),
            1,
        )
        with self.assertRaises(WebhookDeliveryPresentmentQueryError) as orphaned:
            service.present_webhook_delivery(TENANT_ONE, orphan.delivery_attempt_id)
        self.assertEqual(orphaned.exception.rejection_reason_code, "webhook_delivery_not_found")
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.list_webhook_deliveries(TENANT_ONE, page_limit=True)
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.list_webhook_deliveries(TENANT_ONE, page_limit=101)
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.list_webhook_deliveries(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.list_webhook_deliveries(TENANT_ONE, page_limit="abc")
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.list_webhook_deliveries(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/webhook-deliveries")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_webhook_deliveries(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.webhook_deliveries), 1)
        empty_limit = service.list_webhook_deliveries(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.webhook_deliveries), 1)
        self.assertEqual(
            service.list_webhook_deliveries(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.present_webhook_delivery(TENANT_ONE, uuid4())
        with self.assertRaises(WebhookDeliveryPresentmentQueryError):
            service.present_webhook_delivery("", stored.delivery_attempt_id)
        omitted = persist_delivery_attempt(
            ledger,
            TENANT_ONE,
            attempted_at=datetime(2026, 8, 17, 23, 0, tzinfo=UTC),
            failure_reason_code="webhook_http_error",
            payload_suffix="c",
            callback_url="https://hooks.example.test/cwl-failed",
        )[2]
        omitted_presentment = service.present_webhook_delivery(TENANT_ONE, omitted.delivery_attempt_id)
        omitted_payload = omitted_presentment.as_contract_dict()
        self.assertNotIn("http_status", omitted_payload)
        self.assertNotIn("delivered_at", omitted_payload)
        self.assertEqual(omitted_presentment.next_operator_action, "run_deliveries")
        self.assertEqual(listed.webhook_deliveries[0].attempted_at, ATTEMPTED_MORNING)
        self.assertEqual(
            ledger.get_webhook_delivery_attempt(stored.delivery_attempt_id).delivery_attempt_id,
            stored.delivery_attempt_id,
        )
        self.assertEqual(
            ledger.get_webhook_outbox_event(stored.outbox_event_id).outbox_event_id,
            stored.outbox_event_id,
        )
