"""Payment-intent HTTP presentment tests for tenant-scoped collect reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    PaymentIntentPresentmentService,
    PaymentIntentService,
    PaymentSettlementService,
    create_http_app,
)
from metering_billing.contracts import validate_payment_intent_presentment
from metering_billing.errors import PaymentIntentPresentmentQueryError
from metering_billing.payment_intent_presentment import next_operator_action
from test_http_app import invoke_http
from test_invoice_presentment import insert_statement_draft
from test_payment_intent import open_known_morning_case
from test_tax_assessment import HUNDRED, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


class PaymentIntentPresentmentTests(unittest.TestCase):
    """Verify projected payment-intent GET, list envelope, and fail-closed isolation."""

    def test_open_morning_intent_projects_amount_and_record_receipt(self) -> None:
        """A projected known-morning intent shows exact amount and record_receipt."""
        ledger, collection_case_id = open_known_morning_case()
        created = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        assert created.payment_intent_id is not None
        first = PaymentIntentPresentmentService(ledger).present_payment_intent(
            TENANT_ONE, created.payment_intent_id
        )
        second = PaymentIntentPresentmentService(ledger).present_payment_intent(
            TENANT_ONE, created.payment_intent_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.payment_intent_id, created.payment_intent_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.collection_case_id, collection_case_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.payment_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.payment_intent_status, "projected")
        self.assertEqual(first.next_operator_action, "record_receipt")
        payload = first.as_contract_dict()
        self.assertEqual(validate_payment_intent_presentment(payload), ())
        self.assertIsInstance(payload["payment_amount"], str)
        self.assertNotIsInstance(payload["payment_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("payment_intent_outcome_code", payload)
        self.assertNotIn(first.payment_intent_status, {"captured", "settled", "posted"})

    def test_cancelled_intent_waits_and_does_not_offer_a_receipt(self) -> None:
        """A cancelled intent keeps #11 status and waits."""
        ledger, collection_case_id = open_known_morning_case()
        created = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        assert created.payment_intent_id is not None
        PaymentSettlementService(ledger).cancel_payment_intent(
            TENANT_ONE, created.payment_intent_id
        )
        presented = PaymentIntentPresentmentService(ledger).present_payment_intent(
            TENANT_ONE, created.payment_intent_id
        )
        self.assertEqual(presented.payment_intent_status, "cancelled")
        self.assertEqual(presented.payment_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(validate_payment_intent_presentment(presented.as_contract_dict()), ())

    def test_http_post_and_get_return_intent_and_list_envelope(self) -> None:
        """POST is idempotent; GET is 200; list uses {payment_intents, next_cursor}."""
        ledger = seed_rated_ledger()
        first_draft = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        second_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        first_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, first_draft)
        second_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, second_draft)
        assert first_case.collection_case_id is not None
        assert second_case.collection_case_id is not None
        first = PaymentIntentService(
            ledger, clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ).project_payment_intent(TENANT_ONE, first_case.collection_case_id)
        second = PaymentIntentService(
            ledger, clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
        ).project_payment_intent(TENANT_ONE, second_case.collection_case_id)
        assert first.payment_intent_id is not None
        assert second.payment_intent_id is not None
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/payment-intents/{first.payment_intent_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["payment_intent_id"], str(first.payment_intent_id))
        self.assertEqual(body["payment_amount"], "100.00")
        self.assertEqual(body["payment_intent_status"], "projected")
        self.assertEqual(body["next_operator_action"], "record_receipt")
        self.assertEqual(validate_payment_intent_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-intents/{first.payment_intent_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        post_status, post_body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {
                "tenant_reference": TENANT_ONE,
                "collection_case_id": str(first_case.collection_case_id),
            },
        )
        self.assertEqual(post_status, 200)
        self.assertEqual(post_body["payment_intent_outcome_code"], "duplicate_replay")
        self.assertEqual(post_body["payment_intent_id"], str(first.payment_intent_id))

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"payment_intents", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["payment_intents"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["payment_intents"][0]
        self.assertEqual(
            set(first_summary),
            {
                "payment_intent_id",
                "payment_amount",
                "currency_code",
                "payment_intent_status",
                "projected_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["payment_intents"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["payment_intent_id"],
            second_body["payment_intents"][0]["payment_intent_id"],
        }
        self.assertEqual(
            listed_ids, {str(first.payment_intent_id), str(second.payment_intent_id)}
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["payment_intents"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_refuses_card_data_and_provider_secrets(self) -> None:
        """POST must not accept a PAN, CVC, or provider secret on the wire."""
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {
                "tenant_reference": TENANT_ONE,
                "collection_case_id": str(collection_case_id),
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.payment_intents), 0)
        secret_status, secret_body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {
                "tenant_reference": TENANT_ONE,
                "collection_case_id": str(collection_case_id),
                "provider_secret": "sk_test_forbidden",
            },
        )
        self.assertEqual(secret_status, 422)
        self.assertEqual(secret_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.payment_intents), 0)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no intent."""
        ledger, collection_case_id = open_known_morning_case()
        created = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        assert created.payment_intent_id is not None
        payment_intent_id = created.payment_intent_id
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/payment-intents/{payment_intent_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-intents/{payment_intent_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "payment_intent_not_found")
        self.assertNotIn("payment_amount", other_body)
        self.assertNotIn("collection_case_id", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-intents/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "payment_intent_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-intents/{payment_intent_id}",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        with self.assertRaises(PaymentIntentPresentmentQueryError) as missing_tenant:
            PaymentIntentPresentmentService(ledger).present_payment_intent(
                "", payment_intent_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(PaymentIntentPresentmentQueryError) as crossed:
            PaymentIntentPresentmentService(ledger).present_payment_intent(
                TENANT_TWO, payment_intent_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "payment_intent_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action("projected"), "record_receipt")
        self.assertEqual(next_operator_action("cancelled"), "wait")
        self.assertEqual(next_operator_action("rejected"), "wait")
        ledger, collection_case_id = open_known_morning_case()
        created = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        assert created.payment_intent_id is not None
        payment_intent_id = created.payment_intent_id
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/payment-intents/{payment_intent_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.PaymentIntentPresentmentService.present_payment_intent",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/payment-intents/{payment_intent_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = PaymentIntentPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            empty.list_payment_intents(TENANT_ONE)
        service = PaymentIntentPresentmentService(ledger)
        self.assertEqual(
            len(service.list_payment_intents(TENANT_ONE, cursor="").payment_intents), 1
        )
        self.assertEqual(
            len(service.list_payment_intents(TENANT_ONE, page_limit=1).payment_intents), 1
        )
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.list_payment_intents(TENANT_ONE, page_limit=True)
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.list_payment_intents(TENANT_ONE, page_limit=101)
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.list_payment_intents(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.list_payment_intents(TENANT_ONE, page_limit="abc")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/payment-intents")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        put_collection_status, put_collection_body = invoke_http(
            app, "PUT", "/v1/payment-intents"
        )
        self.assertEqual(put_collection_status, 422)
        bad_uuid_status, bad_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/payment-intents/" + ("-" * 36),
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bad_uuid_status, 422)
        self.assertEqual(bad_uuid_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.PaymentIntentPresentmentService.list_payment_intents",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/payment-intents",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_payment_intents(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.payment_intents), 1)
        empty_limit = service.list_payment_intents(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.payment_intents), 1)
        self.assertEqual(service.list_payment_intents(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.list_payment_intents(TENANT_ONE, page_limit="0")
        with self.assertRaises(PaymentIntentPresentmentQueryError):
            service.present_payment_intent(TENANT_ONE, uuid4())
