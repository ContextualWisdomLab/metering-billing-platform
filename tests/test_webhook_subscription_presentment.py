"""Webhook-subscription HTTP presentment tests for metadata-only reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    WebhookSubscriptionPresentmentService,
    WebhookSubscriptionService,
    create_http_app,
)
from metering_billing.contracts import validate_webhook_subscription_presentment
from metering_billing.errors import WebhookSubscriptionPresentmentQueryError
from metering_billing.usage_ledger import StoredWebhookSubscription, generate_record_id
from metering_billing.webhook_outbox import EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED
from metering_billing.webhook_subscription_presentment import next_operator_action
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_webhook_outbox import HTTPS_CALLBACK, _seed_tenants


ISSUED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ISSUED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
EVENT_SET = EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED


def persist_subscription(
    ledger,
    tenant_reference,
    *,
    issued_at,
    subscription_status="active",
    revoked_at=None,
    callback_url=None,
    hash_suffix="a",
):
    """Persist one stored #24 subscription without minting a recoverable secret."""
    tenant = ledger.require_tenant(tenant_reference)
    return ledger.insert_webhook_subscription(
        StoredWebhookSubscription(
            webhook_subscription_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            webhook_subscription_contract_version=1,
            callback_url=callback_url or HTTPS_CALLBACK,
            event_type_set=EVENT_SET,
            webhook_secret_prefix="cwlwh_fake001",
            webhook_secret_hash="hmac-sha256:" + (hash_suffix * 64),
            subscription_status=subscription_status,
            issued_at=issued_at,
            revoked_at=revoked_at,
        )
    )


class WebhookSubscriptionPresentmentTests(unittest.TestCase):
    """Verify metadata GET, list paging, and fail-closed secret isolation."""

    def test_stored_active_projects_run_deliveries_without_secret(self) -> None:
        """An active stored subscription shows run_deliveries and never leaks a secret."""
        ledger = _seed_tenants()
        stored = persist_subscription(ledger, TENANT_ONE, issued_at=ISSUED_MORNING)
        first = WebhookSubscriptionPresentmentService(ledger).present_webhook_subscription(
            TENANT_ONE, stored.webhook_subscription_id
        )
        second = WebhookSubscriptionPresentmentService(ledger).present_webhook_subscription(
            TENANT_ONE, stored.webhook_subscription_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.webhook_subscription_id, stored.webhook_subscription_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.callback_url, HTTPS_CALLBACK)
        self.assertEqual(first.event_type_codes, (EVENT_SET,))
        self.assertEqual(first.subscription_status, "active")
        self.assertEqual(first.webhook_subscription_contract_version, 1)
        self.assertEqual(first.issued_at, ISSUED_MORNING)
        self.assertIsNone(first.revoked_at)
        self.assertEqual(first.next_operator_action, "run_deliveries")
        payload = first.as_contract_dict()
        self.assertEqual(validate_webhook_subscription_presentment(payload), ())
        self.assertNotIn("revoked_at", payload)
        self.assertNotIn("webhook_secret", payload)
        self.assertNotIn("webhook_secret_hash", payload)
        self.assertNotIn("webhook_secret_prefix", payload)
        self.assertNotIn("payload_json", payload)
        self.assertNotIn("hmac-sha256:", str(payload))
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)

    def test_stored_revoked_projects_register(self) -> None:
        """A revoked stored subscription keeps revoked_at and asks to register."""
        ledger = _seed_tenants()
        stored = persist_subscription(
            ledger,
            TENANT_ONE,
            issued_at=ISSUED_MORNING,
            subscription_status="revoked",
            revoked_at=ISSUED_EVENING,
            hash_suffix="b",
            callback_url="https://hooks.example.test/cwl-revoked",
        )
        presented = WebhookSubscriptionPresentmentService(ledger).present_webhook_subscription(
            TENANT_ONE, stored.webhook_subscription_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(presented.next_operator_action, "register")
        self.assertEqual(presented.subscription_status, "revoked")
        self.assertEqual(presented.revoked_at, ISSUED_EVENING)
        self.assertNotIn("webhook_secret", payload)
        self.assertNotIn("webhook_secret_hash", payload)
        self.assertEqual(validate_webhook_subscription_presentment(payload), ())

    def test_http_get_item_and_paged_list_without_secret(self) -> None:
        """GET item and list page stored metadata and never resend a secret."""
        ledger = _seed_tenants()
        times = iter((ISSUED_MORNING, ISSUED_EVENING))
        registrar = WebhookSubscriptionService(ledger, clock=lambda: next(times))
        first = registrar.register_subscription(
            TENANT_ONE, "https://hooks.example.test/cwl-morning", (EVENT_SET,)
        )
        second = registrar.register_subscription(
            TENANT_ONE, "https://hooks.example.test/cwl-evening", (EVENT_SET,)
        )
        secret = first.webhook_secret
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-subscriptions/{first.webhook_subscription_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["webhook_subscription_id"], str(first.webhook_subscription_id))
        self.assertEqual(body["callback_url"], "https://hooks.example.test/cwl-morning")
        self.assertEqual(body["subscription_status"], "active")
        self.assertEqual(body["next_operator_action"], "run_deliveries")
        self.assertNotIn("webhook_secret", body)
        self.assertNotIn("webhook_secret_hash", body)
        self.assertNotIn(secret, str(body))
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-subscriptions/{first.webhook_subscription_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"webhook_subscriptions", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["webhook_subscriptions"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["webhook_subscriptions"][0]
        self.assertEqual(
            set(first_summary),
            {
                "webhook_subscription_id",
                "callback_url",
                "event_type_codes",
                "subscription_status",
                "issued_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["webhook_subscriptions"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["webhook_subscription_id"],
            second_body["webhook_subscriptions"][0]["webhook_subscription_id"],
        }
        self.assertEqual(
            listed_ids,
            {
                str(first.webhook_subscription_id),
                str(second.webhook_subscription_id),
            },
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["webhook_subscriptions"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_register_and_revoke_stay_and_refuse_card_data(self) -> None:
        """POST register and revoke stay #24; PAN and secrets are refused."""
        ledger = _seed_tenants()
        app = create_http_app(ledger)
        register_refused, register_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-subscriptions",
            {
                "tenant_reference": TENANT_ONE,
                "callback_url": HTTPS_CALLBACK,
                "event_type_codes": [EVENT_SET],
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(register_refused, 422)
        self.assertEqual(register_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.webhook_subscriptions), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-subscriptions",
            {
                "tenant_reference": TENANT_ONE,
                "callback_url": HTTPS_CALLBACK,
                "event_type_codes": [EVENT_SET],
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["webhook_subscription_outcome_code"], "accepted")
        self.assertIn("webhook_secret", accepted_body)
        subscription_id = accepted_body["webhook_subscription_id"]
        revoke_refused, revoke_body = invoke_http(
            app,
            "POST",
            f"/v1/webhook-subscriptions/{subscription_id}/revoke",
            {"tenant_reference": TENANT_ONE, "cvc": "123"},
        )
        self.assertEqual(revoke_refused, 422)
        self.assertEqual(revoke_body["rejection_reason_code"], "request_invalid")
        stored_active = ledger.get_webhook_subscription(UUID(subscription_id))
        assert stored_active is not None
        self.assertEqual(stored_active.subscription_status, "active")
        revoke_status, revoke_accepted = invoke_http(
            app,
            "POST",
            f"/v1/webhook-subscriptions/{subscription_id}/revoke",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoke_accepted["subscription_status"], "revoked")
        self.assertNotIn("webhook_secret", revoke_accepted)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no hash."""
        ledger = _seed_tenants()
        stored = persist_subscription(ledger, TENANT_ONE, issued_at=ISSUED_MORNING)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-subscriptions/{stored.webhook_subscription_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-subscriptions/{stored.webhook_subscription_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "webhook_subscription_not_found")
        self.assertNotIn("callback_url", other_body)
        self.assertNotIn("webhook_secret", other_body)
        self.assertNotIn("webhook_secret_hash", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/webhook-subscriptions/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "webhook_subscription_not_found")
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError) as crossed:
            WebhookSubscriptionPresentmentService(ledger).present_webhook_subscription(
                TENANT_TWO, stored.webhook_subscription_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "webhook_subscription_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(subscription_status="active"), "run_deliveries")
        self.assertEqual(next_operator_action(subscription_status="revoked"), "register")
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            next_operator_action(subscription_status="posted")
        ledger = _seed_tenants()
        stored = persist_subscription(ledger, TENANT_ONE, issued_at=ISSUED_MORNING)
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/webhook-subscriptions/{stored.webhook_subscription_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.WebhookSubscriptionPresentmentService.list_webhook_subscriptions",
            side_effect=WebhookSubscriptionPresentmentQueryError("webhook_subscription_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-subscriptions",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "webhook_subscription_not_found")
        with mock.patch(
            "metering_billing.http_app.WebhookSubscriptionPresentmentService.list_webhook_subscriptions",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/webhook-subscriptions",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = WebhookSubscriptionPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            empty.list_webhook_subscriptions(TENANT_ONE)
        service = WebhookSubscriptionPresentmentService(ledger)
        listed = service.list_webhook_subscriptions(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.webhook_subscriptions), 1)
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.list_webhook_subscriptions(TENANT_ONE, page_limit=True)
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.list_webhook_subscriptions(TENANT_ONE, page_limit=101)
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.list_webhook_subscriptions(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.list_webhook_subscriptions(TENANT_ONE, page_limit="abc")
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.list_webhook_subscriptions(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/webhook-subscriptions")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_webhook_subscriptions(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.webhook_subscriptions), 1)
        empty_limit = service.list_webhook_subscriptions(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.webhook_subscriptions), 1)
        self.assertEqual(
            service.list_webhook_subscriptions(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.present_webhook_subscription(TENANT_ONE, uuid4())
        with self.assertRaises(WebhookSubscriptionPresentmentQueryError):
            service.present_webhook_subscription("", stored.webhook_subscription_id)
        self.assertEqual(listed.webhook_subscriptions[0].issued_at, ISSUED_MORNING)
        with mock.patch(
            "metering_billing.http_app.WebhookSubscriptionService.register_subscription",
            side_effect=ValueError("closed"),
        ):
            register_value_status, register_value_body = invoke_http(
                create_http_app(_seed_tenants()),
                "POST",
                "/v1/webhook-subscriptions",
                {
                    "tenant_reference": TENANT_ONE,
                    "callback_url": HTTPS_CALLBACK,
                    "event_type_codes": [EVENT_SET],
                },
            )
        self.assertEqual(register_value_status, 422)
        self.assertEqual(register_value_body["rejection_reason_code"], "request_invalid")
