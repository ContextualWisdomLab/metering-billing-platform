"""Realistic webhook-outbox tests for register, enqueue, sign, and delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock
from urllib.error import URLError
from uuid import uuid4

from metering_billing import (
    AccountingExportService,
    CreditAdjustmentService,
    IssuedCreditNoteService,
    IssuedInvoiceService,
    MemoryUsageLedger,
    PaymentSettlementService,
    WebhookDeliveryService,
    WebhookSubscriptionService,
    create_http_app,
)
from metering_billing.contracts import validate_webhook_delivery, validate_webhook_subscription
from metering_billing.errors import (
    WebhookDeliveryOutcomeCode,
    WebhookDeliveryRejectionReasonCode,
    WebhookSubscriptionOutcomeCode,
    WebhookSubscriptionQueryError,
    WebhookSubscriptionRejectionReasonCode,
)
from metering_billing.usage_ledger import (
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
    StoredWebhookSubscription,
    generate_record_id,
)
from metering_billing.tenant_api_credential import DEFAULT_CREDENTIAL_PEPPER
from metering_billing.webhook_outbox import (
    EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED,
    EVENT_TYPE_CREDIT_NOTE_ISSUED,
    EVENT_TYPE_INVOICE_ISSUED,
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,
    WEBHOOK_SIGNATURE_HEADER,
    WebhookDeliveryResult,
    WebhookSubscriptionResult,
    _all_active_succeeded,
    callback_url_is_allowed,
    canonical_event_type_set,
    enqueue_accepted_fact,
    hash_webhook_secret,
    mint_webhook_secret,
    post_signed_webhook,
    sign_webhook_body,
)
from test_http_app import invoke_http
from test_journal_proposal import draft_known_morning
from test_payment_settlement import project_known_morning_intent
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


ISSUED_AT = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
LOCAL_CALLBACK = "http://127.0.0.1:9/webhook"
HTTPS_CALLBACK = "https://hooks.example.test/cwl"
EVENT_SET = (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,)


class _RecordingHandler(BaseHTTPRequestHandler):
    """Capture one POST body and optional status for signature tests."""

    received: list[tuple[dict[str, str], bytes]] = []
    response_status = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        self.received.append((dict(self.headers), body))
        self.send_response(self.response_status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _start_recorder(status: int = 200) -> tuple[ThreadingHTTPServer, str, list[Any]]:
    """Serve a local HTTP callback and return server, URL, and received posts."""
    received: list[tuple[dict[str, str], bytes]] = []

    class Handler(_RecordingHandler):
        pass

    Handler.received = received
    Handler.response_status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/webhook", received


def _seed_tenants() -> MemoryUsageLedger:
    """Register the two catalog tenants used by commercial tests."""
    ledger = MemoryUsageLedger()
    ledger.register_tenant(TENANT_ONE)
    ledger.register_tenant(TENANT_TWO)
    return ledger


class WebhookOutboxTests(unittest.TestCase):
    """Verify append-only webhook delivery of accepted commercial facts."""

    def test_proposal_accept_enqueues_and_signed_localhost_delivery(self) -> None:
        """A validated proposal must enqueue once and POST a matching HMAC."""
        ledger, invoice_draft_id = draft_known_morning()
        server, callback_url, received = _start_recorder()
        try:
            subscriptions = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
            registered = subscriptions.register_subscription(
                TENANT_ONE, callback_url, EVENT_SET
            )
            self.assertEqual(
                registered.webhook_subscription_outcome_code,
                WebhookSubscriptionOutcomeCode.ACCEPTED,
            )
            self.assertIsNotNone(registered.webhook_secret)
            replay = subscriptions.register_subscription(TENANT_ONE, callback_url, EVENT_SET)
            self.assertEqual(
                replay.webhook_subscription_outcome_code,
                WebhookSubscriptionOutcomeCode.DUPLICATE_REPLAY,
            )
            self.assertEqual(replay.webhook_subscription_id, registered.webhook_subscription_id)
            self.assertIsNone(replay.webhook_secret)
            first = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
            second = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
            pending = ledger.list_pending_webhook_outbox_events(
                ledger.require_tenant(TENANT_ONE).tenant_account_id
            )
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].event_type_code, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
            self.assertEqual(pending[0].source_id, first.proposal_id)
            self.assertEqual(second.proposal_id, first.proposal_id)
            delivered = WebhookDeliveryService(ledger).deliver_due_events(TENANT_ONE)
            self.assertEqual(delivered.webhook_delivery_outcome_code, WebhookDeliveryOutcomeCode.ACCEPTED)
            self.assertEqual(delivered.delivered_event_count, 1)
            self.assertEqual(delivered.failed_delivery_count, 0)
            self.assertEqual(len(received), 1)
            headers, raw_body = received[0]
            envelope = json.loads(raw_body.decode("utf-8"))
            self.assertEqual(envelope["event_type_code"], EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
            self.assertEqual(envelope["tenant_reference"], TENANT_ONE)
            self.assertEqual(envelope["data"]["proposal_id"], str(first.proposal_id))
            self.assertNotIn("webhook_secret", envelope)
            self.assertNotIn("api_credential_secret", json.dumps(envelope))
            expected = sign_webhook_body(registered.webhook_secret or "", raw_body)
            signature = next(
                value
                for key, value in headers.items()
                if key.lower() == WEBHOOK_SIGNATURE_HEADER.lower()
            )
            self.assertEqual(signature, expected)
            listed = subscriptions.list_subscriptions(TENANT_ONE)
            list_json = json.dumps(listed.as_contract_dict())
            self.assertNotIn(registered.webhook_secret or "", list_json)
            self.assertNotIn("webhook_secret_hash", list_json)
            self.assertNotIn("hmac-sha256:", list_json)
            self.assertEqual(validate_webhook_subscription(registered.as_contract_dict()), ())
            self.assertEqual(validate_webhook_subscription(replay.as_contract_dict()), ())
            self.assertEqual(validate_webhook_delivery(delivered.as_contract_dict()), ())
        finally:
            server.shutdown()
            server.server_close()

    def test_revoke_stops_delivery_and_secret_stays_off_list(self) -> None:
        """A revoked subscription must not be POSTed on a later deliver run."""
        ledger, invoice_draft_id = draft_known_morning()
        calls: list[str] = []

        def transport(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, None]:
            calls.append(url)
            del body, headers
            return 200, None

        subscriptions = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
        registered = subscriptions.register_subscription(TENANT_ONE, HTTPS_CALLBACK, EVENT_SET)
        assert registered.webhook_subscription_id is not None
        AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        revoked = subscriptions.revoke_subscription(TENANT_ONE, registered.webhook_subscription_id)
        self.assertEqual(revoked.webhook_subscription_outcome_code, WebhookSubscriptionOutcomeCode.ACCEPTED)
        replay = subscriptions.revoke_subscription(TENANT_ONE, registered.webhook_subscription_id)
        self.assertEqual(replay.webhook_subscription_outcome_code, WebhookSubscriptionOutcomeCode.DUPLICATE_REPLAY)
        delivered = WebhookDeliveryService(ledger, transport=transport).deliver_due_events(TENANT_ONE)
        self.assertEqual(delivered.attempted_delivery_count, 0)
        self.assertEqual(calls, [])
        pending = ledger.list_pending_webhook_outbox_events(
            ledger.require_tenant(TENANT_ONE).tenant_account_id
        )
        self.assertEqual(len(pending), 1)

    def test_payment_and_credit_facts_enqueue_closed_event_types(self) -> None:
        """Applied receipts and recorded credits must enqueue the published types."""
        ledger, payment_intent_id, _collection_case_id = project_known_morning_intent()
        receipt = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL
        )
        tenant = ledger.require_tenant(TENANT_ONE)
        receipt_events = [
            event
            for event in ledger.webhook_outbox_events.values()
            if event.event_type_code == EVENT_TYPE_PAYMENT_RECEIPT_APPLIED
        ]
        self.assertEqual(len(receipt_events), 1)
        self.assertEqual(receipt_events[0].source_id, receipt.payment_receipt_id)
        replay = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, KNOWN_MORNING_TOTAL
        )
        self.assertEqual(replay.payment_receipt_id, receipt.payment_receipt_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in ledger.webhook_outbox_events.values()
                    if event.event_type_code == EVENT_TYPE_PAYMENT_RECEIPT_APPLIED
                ]
            ),
            1,
        )
        draft_ledger, invoice_draft_id = draft_known_morning()
        credit = CreditAdjustmentService(draft_ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "goodwill"
        )
        codes = {event.event_type_code for event in draft_ledger.webhook_outbox_events.values()}
        self.assertIn(EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED, codes)
        self.assertIn(EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, codes)
        self.assertEqual(credit.credit_adjustment_outcome_code.value, "accepted")

    def test_invoice_issued_enqueues_once_and_delivers_signed(self) -> None:
        """First issue enqueues invoice.issued once; replay heals without a second row."""
        ledger, invoice_draft_id = draft_known_morning()
        server, callback_url, received = _start_recorder()
        try:
            subscriptions = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
            registered = subscriptions.register_subscription(
                TENANT_ONE, callback_url, (EVENT_TYPE_INVOICE_ISSUED,)
            )
            self.assertEqual(
                registered.webhook_subscription_outcome_code,
                WebhookSubscriptionOutcomeCode.ACCEPTED,
            )
            self.assertEqual(registered.event_type_codes, (EVENT_TYPE_INVOICE_ISSUED,))
            self.assertEqual(validate_webhook_subscription(registered.as_contract_dict()), ())
            first = IssuedInvoiceService(ledger, clock=lambda: ISSUED_AT).issue_invoice(
                TENANT_ONE, invoice_draft_id
            )
            second = IssuedInvoiceService(ledger, clock=lambda: ISSUED_AT).issue_invoice(
                TENANT_ONE, invoice_draft_id
            )
            issued_events = [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
            ]
            self.assertEqual(first.issued_invoice_outcome_code.value, "accepted")
            self.assertEqual(second.issued_invoice_outcome_code.value, "duplicate_replay")
            self.assertEqual(len(issued_events), 1)
            self.assertEqual(issued_events[0].source_id, first.issued_invoice_id)
            envelope = json.loads(issued_events[0].payload_json)
            self.assertEqual(envelope["event_type_code"], EVENT_TYPE_INVOICE_ISSUED)
            self.assertEqual(envelope["data"]["issued_invoice_id"], str(first.issued_invoice_id))
            self.assertEqual(envelope["data"]["source_payload_hash"], first.source_payload_hash)
            self.assertNotIn("issued_invoice_lines", envelope["data"])
            self.assertNotIn("card_pan", json.dumps(envelope))
            self.assertNotIn("webhook_secret", json.dumps(envelope))
            delivered = WebhookDeliveryService(ledger).deliver_due_events(TENANT_ONE)
            self.assertEqual(delivered.delivered_event_count, 1)
            self.assertEqual(len(received), 1)
            headers, raw_body = received[0]
            posted = json.loads(raw_body.decode("utf-8"))
            self.assertEqual(posted["event_type_code"], EVENT_TYPE_INVOICE_ISSUED)
            expected = sign_webhook_body(registered.webhook_secret or "", raw_body)
            signature = next(
                value
                for key, value in headers.items()
                if key.lower() == WEBHOOK_SIGNATURE_HEADER.lower()
            )
            self.assertEqual(signature, expected)
            orphan_id = issued_events[0].outbox_event_id
            identity = next(
                key
                for key, stored_id in ledger.webhook_outbox_identity_index.items()
                if stored_id == orphan_id
            )
            del ledger.webhook_outbox_events[orphan_id]
            del ledger.webhook_outbox_identity_index[identity]
            healed = IssuedInvoiceService(ledger, clock=lambda: ISSUED_AT).issue_invoice(
                TENANT_ONE, invoice_draft_id
            )
            healed_events = [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
            ]
            self.assertEqual(healed.issued_invoice_outcome_code.value, "duplicate_replay")
            self.assertEqual(len(healed_events), 1)
            self.assertEqual(healed_events[0].source_id, first.issued_invoice_id)
            rejected = IssuedInvoiceService(ledger).issue_invoice(TENANT_TWO, invoice_draft_id)
            self.assertEqual(rejected.issued_invoice_outcome_code.value, "rejected")
            self.assertEqual(
                len(
                    [
                        event
                        for event in ledger.webhook_outbox_events.values()
                        if event.event_type_code == EVENT_TYPE_INVOICE_ISSUED
                    ]
                ),
                1,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_credit_note_issued_enqueues_once_and_delivers_signed(self) -> None:
        """First issue enqueues credit_note.issued once; replay heals without a second row."""
        ledger, invoice_draft_id = draft_known_morning()
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "rating_correction"
        )
        server, callback_url, received = _start_recorder()
        try:
            subscriptions = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
            registered = subscriptions.register_subscription(
                TENANT_ONE, callback_url, (EVENT_TYPE_CREDIT_NOTE_ISSUED,)
            )
            self.assertEqual(
                registered.webhook_subscription_outcome_code,
                WebhookSubscriptionOutcomeCode.ACCEPTED,
            )
            self.assertEqual(registered.event_type_codes, (EVENT_TYPE_CREDIT_NOTE_ISSUED,))
            self.assertEqual(validate_webhook_subscription(registered.as_contract_dict()), ())
            first = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_AT).issue_credit_note(
                TENANT_ONE, credit.credit_adjustment_id
            )
            second = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_AT).issue_credit_note(
                TENANT_ONE, credit.credit_adjustment_id
            )
            issued_events = [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
            ]
            self.assertEqual(first.issued_credit_note_outcome_code.value, "accepted")
            self.assertEqual(second.issued_credit_note_outcome_code.value, "duplicate_replay")
            self.assertEqual(len(issued_events), 1)
            self.assertEqual(issued_events[0].source_id, first.issued_credit_note_id)
            envelope = json.loads(issued_events[0].payload_json)
            self.assertEqual(envelope["event_type_code"], EVENT_TYPE_CREDIT_NOTE_ISSUED)
            data = envelope["data"]
            self.assertEqual(data["issued_credit_note_id"], str(first.issued_credit_note_id))
            self.assertEqual(data["credit_adjustment_id"], str(credit.credit_adjustment_id))
            self.assertEqual(data["invoice_draft_id"], str(credit.invoice_draft_id))
            self.assertEqual(data["source_payload_hash"], first.source_payload_hash)
            self.assertEqual(data["issued_credit_note_contract_version"], 1)
            self.assertEqual(data["currency_code"], "USD")
            self.assertEqual(data["tax_exclusive_amount"], first.as_contract_dict()["tax_exclusive_amount"])
            self.assertEqual(data["tax_amount"], first.as_contract_dict()["tax_amount"])
            self.assertEqual(data["tax_inclusive_amount"], first.as_contract_dict()["tax_inclusive_amount"])
            self.assertEqual(data["issued_credit_note_status"], "issued")
            self.assertEqual(data["issued_at"], first.as_contract_dict()["issued_at"])
            self.assertEqual(data["credit_reason_code"], "rating_correction")
            self.assertNotIn("issued_invoice_id", data)
            self.assertNotIn("issued_credit_note_lines", data)
            self.assertNotIn("credit_note_number", data)
            self.assertNotIn("legal_credit_note_number", data)
            self.assertNotIn("card_pan", json.dumps(envelope))
            self.assertNotIn("webhook_secret", json.dumps(envelope))
            delivered = WebhookDeliveryService(ledger).deliver_due_events(TENANT_ONE)
            self.assertEqual(delivered.delivered_event_count, 1)
            self.assertEqual(len(received), 1)
            headers, raw_body = received[0]
            posted = json.loads(raw_body.decode("utf-8"))
            self.assertEqual(posted["event_type_code"], EVENT_TYPE_CREDIT_NOTE_ISSUED)
            expected = sign_webhook_body(registered.webhook_secret or "", raw_body)
            signature = next(
                value
                for key, value in headers.items()
                if key.lower() == WEBHOOK_SIGNATURE_HEADER.lower()
            )
            self.assertEqual(signature, expected)
            orphan_id = issued_events[0].outbox_event_id
            identity = next(
                key
                for key, stored_id in ledger.webhook_outbox_identity_index.items()
                if stored_id == orphan_id
            )
            del ledger.webhook_outbox_events[orphan_id]
            del ledger.webhook_outbox_identity_index[identity]
            healed = IssuedCreditNoteService(ledger, clock=lambda: ISSUED_AT).issue_credit_note(
                TENANT_ONE, credit.credit_adjustment_id
            )
            healed_events = [
                event
                for event in ledger.webhook_outbox_events.values()
                if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
            ]
            self.assertEqual(healed.issued_credit_note_outcome_code.value, "duplicate_replay")
            self.assertEqual(len(healed_events), 1)
            self.assertEqual(healed_events[0].source_id, first.issued_credit_note_id)
            rejected = IssuedCreditNoteService(ledger).issue_credit_note(
                TENANT_TWO, credit.credit_adjustment_id
            )
            self.assertEqual(rejected.issued_credit_note_outcome_code.value, "rejected")
            self.assertEqual(
                len(
                    [
                        event
                        for event in ledger.webhook_outbox_events.values()
                        if event.event_type_code == EVENT_TYPE_CREDIT_NOTE_ISSUED
                    ]
                ),
                1,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_fail_closed_and_tenant_isolation(self) -> None:
        """Missing tenants, insecure URLs, unknown types, and peers fail closed."""
        ledger = _seed_tenants()
        service = WebhookSubscriptionService(ledger, clock=lambda: ISSUED_AT)
        missing = service.register_subscription("urn:cwl:missing", HTTPS_CALLBACK, EVENT_SET)
        self.assertEqual(missing.rejection_reason_code, WebhookSubscriptionRejectionReasonCode.TENANT_NOT_FOUND)
        insecure = service.register_subscription(TENANT_ONE, "http://example.com/hook", EVENT_SET)
        self.assertEqual(
            insecure.rejection_reason_code,
            WebhookSubscriptionRejectionReasonCode.WEBHOOK_CALLBACK_URL_INSECURE,
        )
        unknown = service.register_subscription(TENANT_ONE, HTTPS_CALLBACK, ("invoice.posted",))
        self.assertEqual(
            unknown.rejection_reason_code,
            WebhookSubscriptionRejectionReasonCode.WEBHOOK_EVENT_TYPE_UNKNOWN,
        )
        empty = service.register_subscription(TENANT_ONE, HTTPS_CALLBACK, ())
        self.assertEqual(
            empty.rejection_reason_code,
            WebhookSubscriptionRejectionReasonCode.WEBHOOK_EVENT_TYPE_UNKNOWN,
        )
        one = service.register_subscription(TENANT_ONE, HTTPS_CALLBACK, EVENT_SET)
        two = WebhookSubscriptionService(ledger).register_subscription(
            TENANT_TWO, "https://hooks.example.test/two", EVENT_SET
        )
        listed_two = WebhookSubscriptionService(ledger).list_subscriptions(TENANT_TWO)
        self.assertEqual(len(listed_two.webhook_subscriptions), 1)
        self.assertEqual(
            listed_two.webhook_subscriptions[0].webhook_subscription_id, two.webhook_subscription_id
        )
        self.assertNotEqual(one.webhook_subscription_id, two.webhook_subscription_id)
        with self.assertRaises(WebhookSubscriptionQueryError) as crossed:
            service.revoke_subscription(TENANT_TWO, one.webhook_subscription_id or uuid4())
        self.assertEqual(crossed.exception.rejection_reason_code, "webhook_subscription_not_found")
        with self.assertRaises(WebhookSubscriptionQueryError):
            service.revoke_subscription(TENANT_ONE, uuid4())
        with self.assertRaises(WebhookSubscriptionQueryError):
            service.list_subscriptions("urn:cwl:missing")
        rejected_delivery = WebhookDeliveryService(ledger).deliver_due_events("urn:cwl:missing")
        self.assertEqual(
            rejected_delivery.rejection_reason_code,
            WebhookDeliveryRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertTrue(callback_url_is_allowed(HTTPS_CALLBACK))
        self.assertTrue(callback_url_is_allowed("http://localhost/hook"))
        self.assertTrue(callback_url_is_allowed("http://127.0.0.1/hook"))
        self.assertTrue(callback_url_is_allowed("http://[::1]/hook"))
        self.assertFalse(callback_url_is_allowed("http://example.com/hook"))
        self.assertFalse(callback_url_is_allowed("https://"))
        self.assertFalse(callback_url_is_allowed(""))
        self.assertFalse(callback_url_is_allowed("ftp://localhost/hook"))

    def test_http_routes_follow_tenant_pin_and_omit_secret_on_list(self) -> None:
        """WSGI register, list, revoke, and deliver must use the #22 key rule."""
        ledger = _seed_tenants()
        app = create_http_app(ledger)
        register_status, register_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-subscriptions",
            {
                "tenant_reference": TENANT_ONE,
                "callback_url": HTTPS_CALLBACK,
                "event_type_codes": list(EVENT_SET),
            },
        )
        self.assertEqual(register_status, 200)
        self.assertIn("webhook_secret", register_body)
        secret = register_body["webhook_secret"]
        subscription_id = register_body["webhook_subscription_id"]
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        self.assertNotIn(secret, json.dumps(list_body))
        for item in list_body["webhook_subscriptions"]:
            self.assertNotIn("webhook_secret", item)
            self.assertNotIn("webhook_secret_hash", item)
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-subscriptions",
            {
                "tenant_reference": TENANT_ONE,
                "callback_url": HTTPS_CALLBACK,
                "event_type_codes": list(EVENT_SET),
            },
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["webhook_subscription_outcome_code"], "duplicate_replay")
        self.assertNotIn("webhook_secret", replay_body)
        deliver_status, deliver_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-deliveries",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(deliver_status, 200)
        self.assertEqual(deliver_body["delivered_event_count"], 0)
        revoke_status, revoke_body = invoke_http(
            app,
            "POST",
            f"/v1/webhook-subscriptions/{subscription_id}/revoke",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoke_body["subscription_status"], "revoked")
        unknown_status, unknown_body = invoke_http(
            app,
            "POST",
            f"/v1/webhook-subscriptions/{uuid4()}/revoke",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "webhook_subscription_not_found")
        method_status, method_body = invoke_http(app, "PUT", "/v1/webhook-subscriptions")
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        put_deliver_status, put_deliver_body = invoke_http(app, "PUT", "/v1/webhook-deliveries")
        self.assertEqual(put_deliver_status, 422)
        missing_status, missing_body = invoke_http(app, "GET", "/v1/webhook-subscriptions")
        self.assertEqual(missing_status, 422)
        insecure_status, insecure_body = invoke_http(
            app,
            "POST",
            "/v1/webhook-subscriptions",
            {
                "tenant_reference": TENANT_ONE,
                "callback_url": "http://example.com/hook",
                "event_type_codes": list(EVENT_SET),
            },
        )
        self.assertEqual(insecure_status, 422)
        self.assertEqual(insecure_body["rejection_reason_code"], "webhook_callback_url_insecure")
        other_status, other_body = invoke_http(
            app,
            "GET",
            "/v1/webhook-subscriptions",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 200)
        self.assertEqual(other_body["webhook_subscriptions"], [])

    def test_retry_failed_delivery_and_coverage_edges(self) -> None:
        """Failed POSTs stay pending; a later deliver increments attempt_number."""
        ledger, invoice_draft_id = draft_known_morning()
        statuses = [500, 200]

        def transport(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str | None]:
            del url, body, headers
            status = statuses.pop(0)
            if status >= 300:
                return status, "webhook_http_error"
            return status, None

        WebhookSubscriptionService(ledger).register_subscription(
            TENANT_ONE, HTTPS_CALLBACK, EVENT_SET
        )
        AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        first = WebhookDeliveryService(ledger, transport=transport).deliver_due_events(TENANT_ONE)
        self.assertEqual(first.failed_delivery_count, 1)
        self.assertEqual(first.delivered_event_count, 0)
        second = WebhookDeliveryService(ledger, transport=transport).deliver_due_events(TENANT_ONE)
        self.assertEqual(second.delivered_event_count, 1)
        tenant = ledger.require_tenant(TENANT_ONE)
        event = ledger.list_pending_webhook_outbox_events(tenant.tenant_account_id)
        self.assertEqual(event, ())
        stored_event = next(iter(ledger.webhook_outbox_events.values()))
        attempts = ledger.list_webhook_delivery_attempts(stored_event.outbox_event_id)
        self.assertEqual([attempt.attempt_number for attempt in attempts], [1, 2])
        third = WebhookDeliveryService(ledger, transport=transport).deliver_due_events(TENANT_ONE)
        self.assertEqual(third.attempted_delivery_count, 0)
        missing_secret_ledger, missing_draft_id = draft_known_morning()
        registered = WebhookSubscriptionService(missing_secret_ledger).register_subscription(
            TENANT_ONE, HTTPS_CALLBACK, EVENT_SET
        )
        assert registered.webhook_subscription_id is not None
        missing_secret_ledger.webhook_subscription_secrets.clear()
        AccountingExportService(missing_secret_ledger).propose_journal(TENANT_ONE, missing_draft_id)
        unavailable = WebhookDeliveryService(missing_secret_ledger, transport=transport).deliver_due_events(
            TENANT_ONE
        )
        self.assertEqual(unavailable.failed_delivery_count, 1)
        unused = WebhookSubscriptionService(ledger).register_subscription(
            TENANT_ONE,
            "https://hooks.example.test/other",
            (EVENT_TYPE_PAYMENT_RECEIPT_APPLIED,),
        )
        self.assertEqual(unused.webhook_subscription_outcome_code, WebhookSubscriptionOutcomeCode.ACCEPTED)
        source_id = uuid4()
        first_enqueue = enqueue_accepted_fact(
            ledger,
            TENANT_ONE,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            source_id,
            {"proposal_id": str(source_id)},
            ISSUED_AT,
        )
        replay_enqueue = enqueue_accepted_fact(
            ledger,
            TENANT_ONE,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            source_id,
            {"proposal_id": str(source_id)},
            ISSUED_AT,
        )
        self.assertIsNotNone(first_enqueue)
        self.assertIs(first_enqueue, replay_enqueue)
        mixed_statuses = [200, 500, 200]

        def mixed_transport(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str | None]:
            del url, body, headers
            status = mixed_statuses.pop(0)
            if status >= 300:
                return status, "webhook_http_error"
            return status, None

        mixed_ledger, mixed_draft_id = draft_known_morning()
        WebhookSubscriptionService(mixed_ledger).register_subscription(
            TENANT_ONE, "https://hooks.example.test/one", EVENT_SET
        )
        WebhookSubscriptionService(mixed_ledger).register_subscription(
            TENANT_ONE, "https://hooks.example.test/two", EVENT_SET
        )
        AccountingExportService(mixed_ledger).propose_journal(TENANT_ONE, mixed_draft_id)
        mixed_first = WebhookDeliveryService(mixed_ledger, transport=mixed_transport).deliver_due_events(
            TENANT_ONE
        )
        self.assertEqual(mixed_first.failed_delivery_count, 1)
        mixed_second = WebhookDeliveryService(mixed_ledger, transport=mixed_transport).deliver_due_events(
            TENANT_ONE
        )
        self.assertEqual(mixed_second.delivered_event_count, 1)
        self.assertEqual(mixed_second.attempted_delivery_count, 1)
        self.assertIsNone(enqueue_accepted_fact(ledger, TENANT_ONE, "invoice.posted", uuid4(), {}, ISSUED_AT))
        self.assertIsNone(
            enqueue_accepted_fact(MemoryUsageLedger(), TENANT_ONE, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, uuid4(), {}, ISSUED_AT)
        )
        with self.assertRaises(ValueError):
            enqueue_accepted_fact(
                ledger,
                TENANT_ONE,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                uuid4(),
                {"api_credential_secret": "leak"},
                ISSUED_AT,
            )
        with self.assertRaises(ValueError):
            enqueue_accepted_fact(
                ledger,
                TENANT_ONE,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                uuid4(),
                {"lines": [{"card_pan": "4111111111111111"}]},
                ISSUED_AT,
            )
        with self.assertRaises(WebhookSubscriptionQueryError):
            canonical_event_type_set("journal_proposal.validated")
        with self.assertRaises(WebhookSubscriptionQueryError):
            canonical_event_type_set(None)
        self.assertEqual(
            canonical_event_type_set(
                (EVENT_TYPE_PAYMENT_RECEIPT_APPLIED, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED)
            ),
            (EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED, EVENT_TYPE_PAYMENT_RECEIPT_APPLIED),
        )
        prefix, secret = mint_webhook_secret()
        self.assertTrue(secret.startswith("cwlwh_"))
        self.assertEqual(prefix, secret[:12])
        self.assertTrue(hash_webhook_secret(secret, DEFAULT_CREDENTIAL_PEPPER).startswith("hmac-sha256:"))
        with self.assertRaises(ValueError):
            hash_webhook_secret("", "pepper")
        with self.assertRaises(ValueError):
            sign_webhook_body("", b"{}")
        with self.assertRaises(ValueError):
            sign_webhook_body("secret", "{}")  # type: ignore[arg-type]
        self.assertEqual(
            sign_webhook_body("secret", b"{}"),
            "sha256=" + hmac.new(b"secret", b"{}", hashlib.sha256).hexdigest(),
        )
        with self.assertRaises(ValueError):
            WebhookSubscriptionService(ledger, credential_pepper="")
        empty_env = MemoryUsageLedger()
        empty_env.register_tenant(TENANT_ONE)
        with mock.patch.dict("os.environ", {"CWL_API_CREDENTIAL_PEPPER": "env_pepper"}):
            env_service = WebhookSubscriptionService(empty_env)
            env_registered = env_service.register_subscription(TENANT_ONE, HTTPS_CALLBACK, EVENT_SET)
        self.assertEqual(env_registered.webhook_subscription_outcome_code, WebhookSubscriptionOutcomeCode.ACCEPTED)
        default_subs = WebhookSubscriptionService()
        default_subs.ledger.register_tenant(TENANT_ONE)
        self.assertEqual(
            default_subs.register_subscription(TENANT_ONE, HTTPS_CALLBACK, EVENT_SET).webhook_subscription_outcome_code,
            WebhookSubscriptionOutcomeCode.ACCEPTED,
        )
        default_delivery = WebhookDeliveryService()
        self.assertEqual(
            default_delivery.deliver_due_events("urn:cwl:missing").rejection_reason_code,
            WebhookDeliveryRejectionReasonCode.TENANT_NOT_FOUND,
        )
        rejected = WebhookSubscriptionResult(
            webhook_subscription_outcome_code=WebhookSubscriptionOutcomeCode.REJECTED,
            webhook_subscription_contract_version=1,
            webhook_subscription_id=None,
            tenant_reference=None,
            callback_url=None,
            event_type_codes=(),
            webhook_secret_prefix=None,
            webhook_secret=None,
            subscription_status=None,
            issued_at=None,
            rejection_reason_code=None,
        )
        self.assertEqual(rejected.as_contract_dict()["rejection_reason_code"], "tenant_not_found")
        with self.assertRaises(ValueError):
            replace(rejected, webhook_subscription_outcome_code="posted").as_contract_dict()  # type: ignore[arg-type]
        delivery_rejected = WebhookDeliveryResult(
            webhook_delivery_outcome_code=WebhookDeliveryOutcomeCode.REJECTED,
            webhook_delivery_contract_version=1,
            delivered_event_count=0,
            attempted_delivery_count=0,
            failed_delivery_count=0,
            rejection_reason_code=None,
        )
        self.assertEqual(delivery_rejected.as_contract_dict()["rejection_reason_code"], "tenant_not_found")
        with self.assertRaises(ValueError):
            replace(delivery_rejected, webhook_delivery_outcome_code="posted").as_contract_dict()  # type: ignore[arg-type]
        self.assertFalse(_all_active_succeeded(ledger, stored_event, ()))
        tenant_one = ledger.require_tenant(TENANT_ONE)
        with self.assertRaises(ValueError):
            ledger.insert_webhook_subscription(
                StoredWebhookSubscription(
                    webhook_subscription_id=generate_record_id(),
                    tenant_account_id=tenant_one.tenant_account_id,
                    webhook_subscription_contract_version=1,
                    callback_url=HTTPS_CALLBACK,
                    event_type_set=EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                    webhook_secret_prefix="cwlwh_xxxxxx",
                    webhook_secret_hash="not-hmac",
                    subscription_status="active",
                    issued_at=ISSUED_AT,
                    revoked_at=None,
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_webhook_subscription(
                StoredWebhookSubscription(
                    webhook_subscription_id=generate_record_id(),
                    tenant_account_id=tenant_one.tenant_account_id,
                    webhook_subscription_contract_version=1,
                    callback_url="https://hooks.example.test/status",
                    event_type_set=EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                    webhook_secret_prefix="cwlwh_xxxxxx",
                    webhook_secret_hash="hmac-sha256:" + ("a" * 64),
                    subscription_status="paused",
                    issued_at=ISSUED_AT,
                    revoked_at=None,
                )
            )
        stored = next(iter(ledger.webhook_subscriptions.values()))
        with self.assertRaises(ValueError):
            ledger.insert_webhook_subscription(stored)
        with self.assertRaises(ValueError):
            ledger.insert_webhook_subscription(
                replace(stored, webhook_subscription_id=generate_record_id(), callback_url="https://dup.test/a")
            )
        with self.assertRaises(ValueError):
            ledger.insert_webhook_subscription(
                replace(
                    stored,
                    webhook_subscription_id=generate_record_id(),
                    webhook_secret_hash="hmac-sha256:" + ("c" * 64),
                )
            )
        with self.assertRaises(ValueError):
            ledger.store_webhook_subscription_secret(stored.webhook_subscription_id, "")
        with self.assertRaises(ValueError):
            ledger.revoke_webhook_subscription(uuid4(), ISSUED_AT)
        self.assertEqual(
            ledger.revoke_webhook_subscription(stored.webhook_subscription_id, ISSUED_AT).subscription_status,
            "revoked",
        )
        self.assertEqual(
            ledger.revoke_webhook_subscription(stored.webhook_subscription_id, ISSUED_AT).subscription_status,
            "revoked",
        )
        with self.assertRaises(ValueError):
            ledger.insert_webhook_outbox_event(
                StoredWebhookOutboxEvent(
                    outbox_event_id=generate_record_id(),
                    tenant_account_id=tenant_one.tenant_account_id,
                    event_type_code=EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                    payload_hash="sha256:" + ("b" * 64),
                    source_id=uuid4(),
                    occurred_at=ISSUED_AT,
                    delivery_status="queued",
                    payload_json="{}",
                    enqueued_at=ISSUED_AT,
                )
            )
        outbox = next(iter(ledger.webhook_outbox_events.values()))
        with self.assertRaises(ValueError):
            ledger.insert_webhook_outbox_event(outbox)
        with self.assertRaises(ValueError):
            ledger.insert_webhook_outbox_event(
                replace(outbox, outbox_event_id=generate_record_id())
            )
        with self.assertRaises(ValueError):
            ledger.mark_webhook_outbox_event_delivered(uuid4())
        marked = ledger.mark_webhook_outbox_event_delivered(outbox.outbox_event_id)
        self.assertEqual(marked.delivery_status, "delivered")
        self.assertEqual(
            ledger.mark_webhook_outbox_event_delivered(outbox.outbox_event_id).delivery_status,
            "delivered",
        )
        with self.assertRaises(ValueError):
            ledger.insert_webhook_delivery_attempt(
                StoredWebhookDeliveryAttempt(
                    delivery_attempt_id=generate_record_id(),
                    outbox_event_id=outbox.outbox_event_id,
                    webhook_subscription_id=stored.webhook_subscription_id,
                    attempt_number=0,
                    http_status=None,
                    delivered_at=None,
                    failure_reason_code="webhook_http_error",
                    attempted_at=ISSUED_AT,
                )
            )
        self.assertIsNone(
            ledger.find_webhook_subscription(tenant_one.tenant_account_id, "https://none.test", "x", 1)
        )
        self.assertIsNone(
            ledger.find_webhook_outbox_event(tenant_one.tenant_account_id, "missing.event", uuid4(), "sha256:x")
        )
        self.assertEqual(ledger.list_webhook_delivery_attempts(uuid4()), ())
        server, callback_url, received = _start_recorder(500)
        try:
            status, failure = post_signed_webhook(callback_url, b"{}", {"Content-Type": "application/json"})
            self.assertEqual(status, 500)
            self.assertEqual(failure, "webhook_http_error")
            self.assertEqual(len(received), 1)
        finally:
            server.shutdown()
            server.server_close()
        refused_status, refused_failure = post_signed_webhook(
            "http://127.0.0.1:1/missing", b"{}", {"Content-Type": "application/json"}
        )
        self.assertIsNone(refused_status)
        self.assertEqual(refused_failure, "webhook_transport_failure")
        with mock.patch(
            "metering_billing.webhook_outbox.urlopen",
            side_effect=URLError("closed"),
        ):
            error_status, error_failure = post_signed_webhook(
                "http://127.0.0.1/hook", b"{}", {"X-Test": "1"}
            )
        self.assertIsNone(error_status)
        self.assertEqual(error_failure, "webhook_transport_failure")
        ok_server, ok_url, ok_received = _start_recorder(200)
        try:
            ok_status, ok_failure = post_signed_webhook(
                ok_url, b'{"ok":true}', {"Content-Type": "application/json"}
            )
            self.assertEqual(ok_status, 200)
            self.assertIsNone(ok_failure)
            self.assertEqual(ok_received[0][1], b'{"ok":true}')
        finally:
            ok_server.shutdown()
            ok_server.server_close()
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
        array_status, array_body = invoke_http(
            create_http_app(ledger),
            "POST",
            "/v1/webhook-subscriptions",
            [],  # type: ignore[arg-type]
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(array_status, 422)
        put_revoke_status, put_revoke_body = invoke_http(
            create_http_app(ledger),
            "PUT",
            f"/v1/webhook-subscriptions/{uuid4()}/revoke",
        )
        self.assertEqual(put_revoke_status, 422)
        self.assertEqual(put_revoke_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(unused.webhook_subscription_outcome_code, WebhookSubscriptionOutcomeCode.ACCEPTED)
