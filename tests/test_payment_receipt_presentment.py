"""Payment-receipt HTTP presentment tests for tenant-scoped settlement reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    PaymentIntentService,
    PaymentReceiptPresentmentService,
    PaymentSettlementService,
    create_http_app,
)
from metering_billing.contracts import validate_payment_receipt_presentment
from metering_billing.errors import PaymentReceiptPresentmentQueryError
from metering_billing.payment_receipt_presentment import next_operator_action
from metering_billing.webhook_outbox import EVENT_TYPE_PAYMENT_RECEIPT_APPLIED
from test_http_app import invoke_http
from test_invoice_presentment import insert_statement_draft
from test_payment_intent import open_known_morning_case
from test_tax_assessment import HUNDRED, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


def apply_known_morning_receipt(received_amount=None):
    """Project the known-morning intent and apply one receipt."""
    ledger, collection_case_id = open_known_morning_case()
    intent = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
    assert intent.payment_intent_id is not None
    amount = KNOWN_MORNING_TOTAL if received_amount is None else received_amount
    receipt = PaymentSettlementService(ledger).record_payment_receipt(
        TENANT_ONE, intent.payment_intent_id, amount
    )
    assert receipt.payment_receipt_id is not None
    return ledger, receipt.payment_receipt_id, intent.payment_intent_id, collection_case_id


class PaymentReceiptPresentmentTests(unittest.TestCase):
    """Verify applied-receipt GET, list envelope, and fail-closed isolation."""

    def test_full_morning_receipt_projects_amount_and_drain_or_wait(self) -> None:
        """A full known-morning receipt shows exact amount and drain_or_wait."""
        ledger, payment_receipt_id, payment_intent_id, collection_case_id = (
            apply_known_morning_receipt()
        )
        first = PaymentReceiptPresentmentService(ledger).present_payment_receipt(
            TENANT_ONE, payment_receipt_id
        )
        second = PaymentReceiptPresentmentService(ledger).present_payment_receipt(
            TENANT_ONE, payment_receipt_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.payment_receipt_id, payment_receipt_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.payment_intent_id, payment_intent_id)
        self.assertEqual(first.collection_case_id, collection_case_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.received_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.remaining_outstanding_amount, Decimal("0"))
        self.assertEqual(first.payment_receipt_status, "applied")
        self.assertEqual(first.collection_case_status, "settled")
        self.assertEqual(first.next_operator_action, "drain_or_wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_payment_receipt_presentment(payload), ())
        self.assertIsInstance(payload["received_amount"], str)
        self.assertNotIsInstance(payload["received_amount"], float)
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("payment_settlement_outcome_code", payload)
        self.assertNotIn(first.payment_receipt_status, {"captured", "posted", "settled"})
        receipt_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_PAYMENT_RECEIPT_APPLIED
        ]
        self.assertEqual(len(receipt_events), 1)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_partial_receipt_keeps_record_receipt_action(self) -> None:
        """A partial receipt leaves outstanding and offers another record_receipt."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt(
            Decimal("0.001")
        )
        presented = PaymentReceiptPresentmentService(ledger).present_payment_receipt(
            TENANT_ONE, payment_receipt_id
        )
        self.assertEqual(presented.payment_receipt_status, "applied")
        self.assertEqual(presented.received_amount, Decimal("0.001"))
        self.assertEqual(presented.remaining_outstanding_amount, KNOWN_MORNING_TOTAL - Decimal("0.001"))
        self.assertEqual(presented.collection_case_status, "open")
        self.assertEqual(presented.next_operator_action, "record_receipt")
        self.assertEqual(validate_payment_receipt_presentment(presented.as_contract_dict()), ())

    def test_http_post_and_get_return_receipt_and_list_envelope(self) -> None:
        """POST stays the #12 write; GET is 200; list uses {payment_receipts, next_cursor}."""
        ledger = seed_rated_ledger()
        first_draft = insert_statement_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        second_draft = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
        first_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, first_draft)
        second_case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, second_draft)
        assert first_case.collection_case_id is not None
        assert second_case.collection_case_id is not None
        first_intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, first_case.collection_case_id
        )
        second_intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, second_case.collection_case_id
        )
        assert first_intent.payment_intent_id is not None
        assert second_intent.payment_intent_id is not None
        first = PaymentSettlementService(
            ledger, clock=lambda: datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ).record_payment_receipt(TENANT_ONE, first_intent.payment_intent_id, HUNDRED)
        second = PaymentSettlementService(
            ledger, clock=lambda: datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
        ).record_payment_receipt(TENANT_ONE, second_intent.payment_intent_id, Decimal("20.00"))
        assert first.payment_receipt_id is not None
        assert second.payment_receipt_id is not None
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/payment-receipts/{first.payment_receipt_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["payment_receipt_id"], str(first.payment_receipt_id))
        self.assertEqual(body["received_amount"], "100.00")
        self.assertEqual(body["remaining_outstanding_amount"], "0.00")
        self.assertEqual(body["payment_receipt_status"], "applied")
        self.assertEqual(body["next_operator_action"], "drain_or_wait")
        self.assertEqual(validate_payment_receipt_presentment(body), ())
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-receipts/{first.payment_receipt_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        post_status, post_body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": str(first_intent.payment_intent_id),
                "received_amount": "100.00",
            },
        )
        self.assertEqual(post_status, 200)
        self.assertEqual(post_body["payment_settlement_outcome_code"], "duplicate_replay")
        self.assertEqual(post_body["payment_receipt_id"], str(first.payment_receipt_id))
        cash = AccountingExportService(ledger).propose_cash_journal(
            TENANT_ONE, first.payment_receipt_id
        )
        self.assertEqual(cash.proposal_status, "validated")
        self.assertNotEqual(cash.proposal_status, "posted")
        self.assertTrue(cash.idempotency_key.startswith(f"{TENANT_ONE}:cash_receipt:"))
        roles = {line.account_role_code for line in cash.proposal_lines}
        self.assertEqual(roles, {"cash_receipt", "accounts_receivable"})

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"payment_receipts", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["payment_receipts"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["payment_receipts"][0]
        self.assertEqual(
            set(first_summary),
            {
                "payment_receipt_id",
                "received_amount",
                "remaining_outstanding_amount",
                "currency_code",
                "payment_receipt_status",
                "received_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["payment_receipts"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["payment_receipt_id"],
            second_body["payment_receipts"][0]["payment_receipt_id"],
        }
        self.assertEqual(
            listed_ids, {str(first.payment_receipt_id), str(second.payment_receipt_id)}
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["payment_receipts"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_refuses_card_data_and_provider_secrets(self) -> None:
        """POST must not accept a PAN, CVC, or provider secret on the wire."""
        ledger, collection_case_id = open_known_morning_case()
        intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_ONE, collection_case_id
        )
        assert intent.payment_intent_id is not None
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": str(intent.payment_intent_id),
                "received_amount": str(KNOWN_MORNING_TOTAL),
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.payment_receipts), 0)
        secret_status, secret_body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": str(intent.payment_intent_id),
                "received_amount": str(KNOWN_MORNING_TOTAL),
                "cvc": "123",
            },
        )
        self.assertEqual(secret_status, 422)
        self.assertEqual(secret_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.payment_receipts), 0)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no receipt."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/payment-receipts/{payment_receipt_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-receipts/{payment_receipt_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "payment_receipt_not_found")
        self.assertNotIn("received_amount", other_body)
        self.assertNotIn("collection_case_id", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-receipts/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "payment_receipt_not_found")
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            f"/v1/payment-receipts/{payment_receipt_id}",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        with self.assertRaises(PaymentReceiptPresentmentQueryError) as missing_tenant:
            PaymentReceiptPresentmentService(ledger).present_payment_receipt(
                "", payment_receipt_id
            )
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(PaymentReceiptPresentmentQueryError) as crossed:
            PaymentReceiptPresentmentService(ledger).present_payment_receipt(
                TENANT_TWO, payment_receipt_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "payment_receipt_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(Decimal("0")), "drain_or_wait")
        self.assertEqual(next_operator_action(Decimal("1.00")), "record_receipt")
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/payment-receipts/{payment_receipt_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.PaymentReceiptPresentmentService.present_payment_receipt",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/payment-receipts/{payment_receipt_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = PaymentReceiptPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            empty.list_payment_receipts(TENANT_ONE)
        service = PaymentReceiptPresentmentService(ledger)
        self.assertEqual(
            len(service.list_payment_receipts(TENANT_ONE, cursor="").payment_receipts), 1
        )
        self.assertEqual(
            len(service.list_payment_receipts(TENANT_ONE, page_limit=1).payment_receipts), 1
        )
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.list_payment_receipts(TENANT_ONE, page_limit=True)
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.list_payment_receipts(TENANT_ONE, page_limit=101)
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.list_payment_receipts(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.list_payment_receipts(TENANT_ONE, page_limit="abc")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/payment-receipts")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        put_collection_status, _put_collection_body = invoke_http(
            app, "PUT", "/v1/payment-receipts"
        )
        self.assertEqual(put_collection_status, 422)
        bad_uuid_status, bad_uuid_body = invoke_http(
            app,
            "GET",
            "/v1/payment-receipts/" + ("-" * 36),
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bad_uuid_status, 422)
        self.assertEqual(bad_uuid_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.PaymentReceiptPresentmentService.list_payment_receipts",
            side_effect=ValueError("closed"),
        ):
            decimal_status, decimal_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/payment-receipts",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(decimal_status, 422)
        self.assertEqual(decimal_body["rejection_reason_code"], "request_invalid")
        default_limit = service.list_payment_receipts(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.payment_receipts), 1)
        empty_limit = service.list_payment_receipts(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.payment_receipts), 1)
        self.assertEqual(service.list_payment_receipts(TENANT_ONE, page_limit=50).next_cursor, None)
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.list_payment_receipts(TENANT_ONE, page_limit="0")
        with self.assertRaises(PaymentReceiptPresentmentQueryError):
            service.present_payment_receipt(TENANT_ONE, uuid4())
        with mock.patch.object(ledger, "get_collection_case", return_value=None):
            with self.assertRaises(PaymentReceiptPresentmentQueryError) as orphan:
                service.present_payment_receipt(TENANT_ONE, payment_receipt_id)
        self.assertEqual(orphan.exception.rejection_reason_code, "payment_receipt_not_found")
