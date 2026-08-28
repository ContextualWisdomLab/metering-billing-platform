"""HTTP accept-surface tests for the already-built commercial billing path."""

from __future__ import annotations

import io
import json
import os
import runpy
import signal
import unittest
from decimal import Decimal
from typing import Any, Mapping
from unittest import mock
from urllib.parse import urlencode
from uuid import uuid4

from metering_billing import MemoryUsageLedger, format_exact_decimal
from metering_billing.accounting_export import AccountingExportService
from metering_billing.collection_case import CollectionCaseService
from metering_billing.contracts import (
    validate_collection_case,
    validate_invoice_draft,
    validate_journal_proposal,
    validate_payment_intent,
    validate_payment_receipt,
    validate_rating_run,
    validate_usage_ingestion_receipt,
)
from metering_billing.http_app import (
    HttpRequestError,
    _dispatch_write,
    _parse_uuid,
    _send_json,
    _status_for_contract,
    _status_for_result,
    create_http_app,
    main,
    ThreadingWSGIServer,
)
from metering_billing.invoice_draft import InvoiceDraftService
from metering_billing.payment_intent import PaymentIntentService
from metering_billing.payment_settlement import PaymentSettlementService
from metering_billing.usage_ingestion import UsageIngestionService
from metering_billing.usage_rating import UsageRatingService
from test_payment_intent import open_known_morning_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, known_event_batch, make_event
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


KNOWN_TOTAL_TEXT = format_exact_decimal(KNOWN_MORNING_TOTAL)


def invoke_http(
    app: Any,
    method: str,
    path: str,
    payload: Mapping[str, object] | bytes | None = None,
    extra_environ: Mapping[str, object] | None = None,
    query: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call the WSGI app in-process and return status plus JSON body."""
    if payload is None:
        raw_body = b""
    elif isinstance(payload, bytes):
        raw_body = payload
    else:
        raw_body = json.dumps(payload).encode("utf-8")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": urlencode(query) if query else "",
        "CONTENT_LENGTH": str(len(raw_body)),
        "wsgi.input": io.BytesIO(raw_body),
        "wsgi.errors": io.StringIO(),
        "wsgi.url_scheme": "http",
    }
    if headers is not None:
        for header_name, header_value in headers.items():
            environ["HTTP_" + header_name.upper().replace("-", "_")] = header_value
    if extra_environ is not None:
        environ.update(extra_environ)
    recorded: dict[str, Any] = {}

    def start_response(
        status: str,
        headers: list[tuple[str, str]],
        exc_info: object | None = None,
    ) -> Any:
        recorded["status"] = status
        recorded["headers"] = headers
        return lambda _data: None

    chunks = list(app(environ, start_response))
    body = json.loads(b"".join(chunks).decode("utf-8"))
    return int(str(recorded["status"]).split()[0]), body


def _assert_decimal_strings(testcase: unittest.TestCase, payload: Mapping[str, Any]) -> None:
    """Require money-like fields to stay exact-decimal strings, never floats."""
    money_keys = {
        "rated_total_amount",
        "drafted_total_amount",
        "outstanding_amount",
        "payment_amount",
        "received_amount",
        "remaining_outstanding_amount",
        "debit_amount",
        "credit_amount",
        "line_total_amount",
        "unit_price_amount",
        "rated_quantity",
    }
    for key, value in payload.items():
        if key in money_keys:
            testcase.assertIsInstance(value, str)
            testcase.assertNotIsInstance(value, float)
        elif isinstance(value, dict):
            _assert_decimal_strings(testcase, value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_decimal_strings(testcase, item)


class HttpAcceptSurfaceTests(unittest.TestCase):
    """Verify JSON HTTP is a thin adapter over the existing commercial services."""

    def test_full_commercial_path_accepts_exact_decimal_json(self) -> None:
        """Buyers can walk usage through cash journal over HTTP without floats."""
        app = create_http_app(seed_rated_ledger())
        events = list(known_event_batch())
        ingest_status, ingest_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE, "events": events},
        )
        self.assertEqual(ingest_status, 200)
        self.assertEqual(ingest_body["accepted_event_count"], 3)
        self.assertEqual(validate_usage_ingestion_receipt(ingest_body), ())

        rating_status, rating_body = invoke_http(
            app,
            "POST",
            "/v1/rating-runs",
            {
                "tenant_reference": TENANT_ONE,
                "window_started_at": "2026-08-16T10:00:00Z",
                "window_ended_at": "2026-08-16T11:00:00Z",
                "rate_card_version": 1,
            },
        )
        self.assertEqual(rating_status, 200)
        self.assertEqual(rating_body["rating_outcome_code"], "accepted")
        self.assertEqual(rating_body["rated_total_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(validate_rating_run(rating_body), ())
        rating_run_id = rating_body["rating_run_id"]

        draft_status, draft_body = invoke_http(
            app,
            "POST",
            "/v1/invoice-drafts",
            {"tenant_reference": TENANT_ONE, "rating_run_id": rating_run_id},
        )
        self.assertEqual(draft_status, 200)
        self.assertEqual(draft_body["invoice_draft_outcome_code"], "accepted")
        self.assertEqual(draft_body["drafted_total_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(validate_invoice_draft(draft_body), ())
        invoice_draft_id = draft_body["invoice_draft_id"]

        journal_status, journal_body = invoke_http(
            app,
            "POST",
            "/v1/journal-proposals",
            {"tenant_reference": TENANT_ONE, "invoice_draft_id": invoice_draft_id},
        )
        self.assertEqual(journal_status, 200)
        self.assertEqual(journal_body["proposal_status"], "validated")
        self.assertNotEqual(journal_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", journal_body)
        self.assertEqual(validate_journal_proposal(journal_body), ())

        case_status, case_body = invoke_http(
            app,
            "POST",
            "/v1/collection-cases",
            {"tenant_reference": TENANT_ONE, "invoice_draft_id": invoice_draft_id},
        )
        self.assertEqual(case_status, 200)
        self.assertEqual(case_body["collection_case_outcome_code"], "accepted")
        self.assertEqual(case_body["outstanding_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(validate_collection_case(case_body), ())
        collection_case_id = case_body["collection_case_id"]

        dunning_status, dunning_body = invoke_http(
            app,
            "POST",
            f"/v1/collection-cases/{collection_case_id}/dunning-events",
            {"tenant_reference": TENANT_ONE, "dunning_notice_code": "first_notice"},
        )
        self.assertEqual(dunning_status, 200)
        self.assertEqual(dunning_body["collection_case_outcome_code"], "accepted")
        self.assertEqual(dunning_body["collection_case_status"], "dunning")

        intent_status, intent_body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {"tenant_reference": TENANT_ONE, "collection_case_id": collection_case_id},
        )
        self.assertEqual(intent_status, 200)
        self.assertEqual(intent_body["payment_intent_outcome_code"], "accepted")
        self.assertEqual(intent_body["payment_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(validate_payment_intent(intent_body), ())
        payment_intent_id = intent_body["payment_intent_id"]

        receipt_status, receipt_body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": payment_intent_id,
                "received_amount": KNOWN_TOTAL_TEXT,
            },
        )
        self.assertEqual(receipt_status, 200)
        self.assertEqual(receipt_body["payment_settlement_outcome_code"], "accepted")
        self.assertEqual(receipt_body["received_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(Decimal(receipt_body["remaining_outstanding_amount"]), Decimal("0"))
        self.assertEqual(validate_payment_receipt(receipt_body), ())
        payment_receipt_id = receipt_body["payment_receipt_id"]

        cash_status, cash_body = invoke_http(
            app,
            "POST",
            "/v1/cash-journal-proposals",
            {"tenant_reference": TENANT_ONE, "payment_receipt_id": payment_receipt_id},
        )
        self.assertEqual(cash_status, 200)
        self.assertEqual(cash_body["proposal_status"], "validated")
        self.assertNotEqual(cash_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", cash_body)
        self.assertEqual(cash_body["lines"][0]["account_role_code"], "cash_receipt")
        self.assertEqual(cash_body["lines"][0]["debit_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(cash_body["lines"][1]["account_role_code"], "accounts_receivable")
        self.assertEqual(cash_body["lines"][1]["credit_amount"], KNOWN_TOTAL_TEXT)
        self.assertEqual(validate_journal_proposal(cash_body), ())
        self.assertNotEqual(cash_body["proposal_id"], journal_body["proposal_id"])

        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/cash-journal-proposals",
            {"tenant_reference": TENANT_ONE, "payment_receipt_id": payment_receipt_id},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["proposal_id"], cash_body["proposal_id"])
        self.assertEqual(replay_body["proposal_status"], "validated")

        for payload in (
            ingest_body,
            rating_body,
            draft_body,
            journal_body,
            case_body,
            intent_body,
            receipt_body,
            cash_body,
        ):
            _assert_decimal_strings(self, payload)

        self.assertEqual(KNOWN_MORNING_TOTAL, type(KNOWN_MORNING_TOTAL)(KNOWN_TOTAL_TEXT))

    def test_single_usage_event_and_replays_return_200(self) -> None:
        """A single event write and its replay stay HTTP 200."""
        app = create_http_app(seed_rated_ledger())
        event = make_event()
        first_status, first_body = invoke_http(app, "POST", "/v1/usage-events", event)
        second_status, second_body = invoke_http(app, "POST", "/v1/usage-events", event)
        self.assertEqual(first_status, 200)
        self.assertEqual(first_body["ingestion_outcome_code"], "accepted")
        self.assertEqual(second_status, 200)
        self.assertEqual(second_body["ingestion_outcome_code"], "duplicate_replay")
        self.assertEqual(second_body["usage_event_id"], first_body["usage_event_id"])

    def test_mixed_and_all_rejected_usage_batches_map_status(self) -> None:
        """A mixed batch stays 200; an all-rejected batch becomes 422."""
        app = create_http_app(seed_rated_ledger())
        mixed_status, mixed_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {
                "tenant_reference": TENANT_ONE,
                "events": [make_event(), {"tenant_reference": TENANT_ONE}],
            },
        )
        self.assertEqual(mixed_status, 200)
        self.assertEqual(mixed_body["accepted_event_count"], 1)
        self.assertEqual(mixed_body["rejected_event_count"], 1)

        rejected_status, rejected_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE, "events": [{"tenant_reference": TENANT_ONE}]},
        )
        self.assertEqual(rejected_status, 422)
        self.assertEqual(rejected_body["accepted_event_count"], 0)
        self.assertEqual(rejected_body["rejected_event_count"], 1)

    def test_cross_tenant_writes_are_rejected_without_leaking_ids(self) -> None:
        """Another tenant cannot settle or export the first tenant's documents."""
        app = create_http_app(seed_rated_ledger())
        events = list(known_event_batch())
        invoke_http(app, "POST", "/v1/usage-events", {"tenant_reference": TENANT_ONE, "events": events})
        _, rating_body = invoke_http(
            app,
            "POST",
            "/v1/rating-runs",
            {
                "tenant_reference": TENANT_ONE,
                "window_started_at": "2026-08-16T10:00:00Z",
                "window_ended_at": "2026-08-16T11:00:00Z",
                "rate_card_version": 1,
            },
        )
        status, body = invoke_http(
            app,
            "POST",
            "/v1/invoice-drafts",
            {"tenant_reference": TENANT_TWO, "rating_run_id": rating_body["rating_run_id"]},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["invoice_draft_outcome_code"], "rejected")
        self.assertNotIn("invoice_draft_id", body)

    def test_float_received_amount_is_rejected(self) -> None:
        """Binary floating-point money cannot enter the HTTP settlement path."""
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        _, intent_body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {"tenant_reference": TENANT_ONE, "collection_case_id": str(collection_case_id)},
        )
        status, body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": intent_body["payment_intent_id"],
                "received_amount": 0.003705,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["payment_settlement_outcome_code"], "rejected")

    def test_cancel_then_receipt_is_rejected(self) -> None:
        """HTTP cancel flips the intent; a later receipt cannot apply."""
        ledger, collection_case_id = open_known_morning_case()
        app = create_http_app(ledger)
        _, intent_body = invoke_http(
            app,
            "POST",
            "/v1/payment-intents",
            {"tenant_reference": TENANT_ONE, "collection_case_id": str(collection_case_id)},
        )
        payment_intent_id = intent_body["payment_intent_id"]
        cancel_status, cancel_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-intents/{payment_intent_id}/cancel",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(cancel_status, 200)
        self.assertEqual(cancel_body["payment_settlement_outcome_code"], "accepted")
        self.assertEqual(cancel_body["payment_intent_status"], "cancelled")
        receipt_status, receipt_body = invoke_http(
            app,
            "POST",
            "/v1/payment-receipts",
            {
                "tenant_reference": TENANT_ONE,
                "payment_intent_id": payment_intent_id,
                "received_amount": KNOWN_TOTAL_TEXT,
            },
        )
        self.assertEqual(receipt_status, 422)
        self.assertEqual(receipt_body["payment_settlement_outcome_code"], "rejected")

    def test_healthz_and_unknown_or_wrong_method_routes(self) -> None:
        """Health is GET 200; unknown paths are 404; wrong methods stay 422."""
        app = create_http_app()
        health_status, health_body = invoke_http(app, "GET", "/healthz")
        self.assertEqual(health_status, 200)
        self.assertEqual(health_body, {"status": "ok"})

        missing_method_status, missing_method_body = invoke_http(
            app,
            "GET",
            "/healthz",
            extra_environ={"REQUEST_METHOD": "", "PATH_INFO": "/healthz"},
        )
        self.assertEqual(missing_method_status, 200)
        self.assertEqual(missing_method_body, {"status": "ok"})

        unknown_status, unknown_body = invoke_http(app, "POST", "/v1/not-a-route", {"tenant_reference": TENANT_ONE})
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "route_not_found")

        empty_path_status, empty_path_body = invoke_http(
            app,
            "GET",
            "/",
            extra_environ={"PATH_INFO": ""},
        )
        self.assertEqual(empty_path_status, 404)
        self.assertEqual(empty_path_body["rejection_reason_code"], "route_not_found")

        for method, path in (
            ("POST", "/healthz"),
            ("PUT", "/v1/usage-events"),
            ("PUT", "/v1/rating-runs"),
            ("GET", f"/v1/collection-cases/{uuid4()}/dunning-events"),
            ("GET", f"/v1/payment-intents/{uuid4()}/cancel"),
        ):
            status, body = invoke_http(app, method, path, {"tenant_reference": TENANT_ONE})
            self.assertEqual(status, 422, msg=f"{method} {path}")
            self.assertEqual(body["rejection_reason_code"], "request_invalid")

    def test_adapter_rejects_invalid_json_tenant_and_types(self) -> None:
        """Malformed bodies and missing tenants fail at the adapter, not the service."""
        app = create_http_app(seed_rated_ledger())
        cases: tuple[tuple[str, str, Mapping[str, object] | bytes | None, Mapping[str, object] | None, str], ...] = (
            ("POST", "/v1/usage-events", None, None, "request_invalid"),
            ("POST", "/v1/usage-events", b"{", None, "request_invalid"),
            ("POST", "/v1/usage-events", b"\xff", None, "request_invalid"),
            ("POST", "/v1/usage-events", b"[]", None, "request_invalid"),
            ("POST", "/v1/usage-events", {"events": []}, None, "tenant_not_found"),
            ("POST", "/v1/usage-events", {"tenant_reference": ""}, None, "tenant_not_found"),
            ("POST", "/v1/usage-events", {"tenant_reference": 1}, None, "tenant_not_found"),
            (
                "POST",
                "/v1/rating-runs",
                {
                    "tenant_reference": TENANT_ONE,
                    "window_started_at": "2026-08-16T10:00:00Z",
                    "window_ended_at": "2026-08-16T11:00:00Z",
                    "rate_card_version": "1",
                },
                None,
                "request_invalid",
            ),
            (
                "POST",
                "/v1/rating-runs",
                {
                    "tenant_reference": TENANT_ONE,
                    "window_started_at": "2026-08-16T10:00:00Z",
                    "window_ended_at": "2026-08-16T11:00:00Z",
                    "rate_card_version": True,
                },
                None,
                "request_invalid",
            ),
            (
                "POST",
                "/v1/rating-runs",
                {
                    "tenant_reference": TENANT_ONE,
                    "window_started_at": "not-a-timestamp",
                    "window_ended_at": "2026-08-16T11:00:00Z",
                    "rate_card_version": 1,
                },
                None,
                "request_invalid",
            ),
            (
                "POST",
                "/v1/invoice-drafts",
                {"tenant_reference": TENANT_ONE, "rating_run_id": 1},
                None,
                "request_invalid",
            ),
            (
                "POST",
                "/v1/invoice-drafts",
                {"tenant_reference": TENANT_ONE, "rating_run_id": "not-a-uuid"},
                None,
                "request_invalid",
            ),
            (
                "POST",
                f"/v1/collection-cases/{uuid4()}/dunning-events",
                {"tenant_reference": TENANT_ONE, "dunning_notice_code": 1},
                None,
                "request_invalid",
            ),
        )
        for method, path, payload, extra, reason in cases:
            status, body = invoke_http(app, method, path, payload, extra)
            self.assertEqual(status, 422, msg=f"{path} {payload!r}")
            self.assertEqual(body["rejection_reason_code"], reason)

        length_status, length_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE},
            extra_environ={"CONTENT_LENGTH": "abc"},
        )
        self.assertEqual(length_status, 422)
        self.assertEqual(length_body["rejection_reason_code"], "request_invalid")

        negative_status, negative_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE},
            extra_environ={"CONTENT_LENGTH": "-1"},
        )
        self.assertEqual(negative_status, 422)
        self.assertEqual(negative_body["rejection_reason_code"], "request_invalid")

        missing_input_status, missing_input_body = invoke_http(
            app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE},
            extra_environ={"wsgi.input": None},
        )
        self.assertEqual(missing_input_status, 422)
        self.assertEqual(missing_input_body["rejection_reason_code"], "request_invalid")

    def test_default_app_without_catalog_rejects_writes(self) -> None:
        """An empty in-memory ledger cannot accept a tenant-scoped usage write."""
        app = create_http_app()
        status, body = invoke_http(app, "POST", "/v1/usage-events", make_event())
        self.assertEqual(status, 422)
        self.assertEqual(body["ingestion_outcome_code"], "rejected")

    def test_unknown_dispatch_and_status_helpers(self) -> None:
        """Internal helpers fail closed when a route or outcome is missing."""
        ledger = MemoryUsageLedger()
        with self.assertRaises(HttpRequestError) as error:
            _dispatch_write(
                "not_a_route",
                {},
                TENANT_ONE,
                {"tenant_reference": TENANT_ONE},
                UsageIngestionService(ledger),
                UsageRatingService(ledger),
                InvoiceDraftService(ledger),
                AccountingExportService(ledger),
                CollectionCaseService(ledger),
                PaymentIntentService(ledger),
                PaymentSettlementService(ledger),
            )
        self.assertEqual(error.exception.rejection_reason_code, "request_invalid")
        self.assertEqual(_status_for_contract({"rating_outcome_code": "accepted"}), 200)
        self.assertEqual(_status_for_contract({"rating_outcome_code": "rejected"}), 422)
        self.assertEqual(_status_for_contract({"note": "no outcome"}), 422)
        self.assertEqual(
            _status_for_contract({"proposal_id": str(uuid4()), "proposal_status": "validated"}),
            200,
        )
        self.assertEqual(
            _status_for_contract({"proposal_id": str(uuid4()), "proposal_status": "rejected"}),
            422,
        )
        accepted = type("AcceptedResult", (), {"journal_proposal_outcome_code": "accepted"})()
        rejected = type("RejectedResult", (), {"journal_proposal_outcome_code": "rejected"})()
        empty = type("EmptyResult", (), {})()
        self.assertEqual(_status_for_result(accepted), 200)
        self.assertEqual(_status_for_result(rejected), 422)
        self.assertEqual(_status_for_result(empty), 422)
        known_id = uuid4()
        self.assertEqual(_parse_uuid(str(known_id), "rating_run_id"), known_id)

        recorded: dict[str, str] = {}

        def start_response(status: str, headers: list[tuple[str, str]], exc_info: object | None = None) -> Any:
            recorded["status"] = status
            return lambda _data: None

        list(_send_json(start_response, 201, {"ok": True}))
        self.assertEqual(recorded["status"], "201 OK")

    def test_main_binds_all_interfaces_and_module_entrypoint(self) -> None:
        """Standalone serving binds 0.0.0.0 and $PORT, including the module entry."""
        fake_server = mock.Mock()
        with mock.patch("metering_billing.http_app.make_server", return_value=fake_server) as maker:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PORT", None)
                self.assertEqual(main(()), 0)
        host, port, app = maker.call_args.args
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(port, 8000)
        self.assertIs(maker.call_args.kwargs["server_class"], ThreadingWSGIServer)
        self.assertTrue(callable(app))
        fake_server.serve_forever.assert_called_once()
        fake_server.server_close.assert_called_once()

        shutdown_server = mock.Mock()
        registered_handlers: dict[int, Any] = {}

        def register_handler(signal_number: int, handler: Any) -> object:
            registered_handlers[signal_number] = handler
            return object()

        def serve_until_shutdown() -> None:
            registered_handlers[signal.SIGTERM](signal.SIGTERM, None)
            registered_handlers[signal.SIGTERM](signal.SIGTERM, None)

        shutdown_server.serve_forever.side_effect = serve_until_shutdown
        with mock.patch("metering_billing.http_app.signal.signal", side_effect=register_handler):
            with mock.patch("metering_billing.http_app.make_server", return_value=shutdown_server):
                with mock.patch(
                    "metering_billing.http_app.threading.Thread"
                ) as shutdown_thread:
                    shutdown_thread.return_value.start.side_effect = (
                        lambda: shutdown_server.shutdown()
                    )
                    self.assertEqual(main(()), 0)
        shutdown_server.shutdown.assert_called_once()
        shutdown_server.server_close.assert_called_once()
        shutdown_thread.assert_called_once()
        shutdown_thread.return_value.start.assert_called_once()

        fake_server_two = mock.Mock()
        with mock.patch("metering_billing.http_app.make_server", return_value=fake_server_two) as maker_two:
            sentinel_ledger = object()
            with mock.patch(
                "metering_billing.http_app.create_default_ledger", return_value=sentinel_ledger
            ) as ledger_maker:
                with mock.patch("metering_billing.http_app.create_http_app") as creator:
                    creator.return_value = lambda environ, start_response: []
                    with mock.patch.dict(
                        os.environ,
                        {"PORT": "9000", "AIS_BASE_URL": "http://ais.example"},
                        clear=False,
                    ):
                        self.assertEqual(main(None), 0)
        self.assertEqual(maker_two.call_args.args[1], 9000)
        creator.assert_called_once_with(sentinel_ledger, ais_base_url="http://ais.example")
        ledger_maker.assert_called_once_with()

        fake_server_three = mock.Mock()
        with mock.patch("wsgiref.simple_server.make_server", return_value=fake_server_three):
            with mock.patch.dict(os.environ, {"PORT": "8000"}, clear=False):
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_module("metering_billing.http_app", run_name="__main__")
        self.assertEqual(exited.exception.code, 0)
        fake_server_three.serve_forever.assert_called_once()


if __name__ == "__main__":
    unittest.main()
