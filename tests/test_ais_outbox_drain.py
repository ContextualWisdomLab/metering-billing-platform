"""AIS outbox drain tests: URN equality, #16 receipt lookup, and publish."""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from uuid import UUID, uuid4
from wsgiref.simple_server import make_server

from metering_billing import (
    AisOutboxDrainService,
    CreditAdjustmentService,
    MemoryUsageLedger,
    PostingReceiptPullService,
    create_http_app,
)
from metering_billing.contracts import validate_ais_outbox_drain
from metering_billing.errors import (
    AisOutboxDrainOutcomeCode,
    AisOutboxDrainRejectionReasonCode,
)
from metering_billing.posting_receipt import (
    AisLookupResult,
    AisOutboxEvent,
    AisOutboxPage,
    AisPostingReceiptClient,
    AisTransportError,
    _parse_outbox_page,
    ais_base_url_is_allowed,
    general_journal_aggregate_reference,
    posting_receipt_payload_reference,
)
from metering_billing.ais_outbox_drain import AisOutboxDrainResult
from test_credit_adjustment import PARTIAL_CREDIT_AMOUNT
from test_http_app import invoke_http
from test_journal_proposal import draft_known_morning
from test_posting_receipt_observation import (
    make_ais_receipt,
    persist_known_ar_and_cash_proposals,
)
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


EXAMPLE_PROPOSAL_ID = UUID("11111111-1111-1111-1111-111111111111")


class FakeAisOutboxState:
    """In-process AIS outbox, receipt, and publish table."""

    def __init__(self) -> None:
        self.pages: list[dict[str, object]] = []
        self.receipts: dict[tuple[str, str], dict[str, object]] = {}
        self.publish_status: dict[tuple[str, str], int] = {}
        self.list_status: int = 200
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.published: list[str] = []


def start_fake_ais_outbox(state: FakeAisOutboxState) -> Any:
    """Serve AIS outbox, publish, and posting-receipt routes locally."""

    def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        tenant = environ.get("HTTP_X_CWL_TENANT_REFERENCE")
        method = str(environ.get("REQUEST_METHOD") or "GET")
        path = str(environ.get("PATH_INFO") or "")
        query = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        key = (query.get("idempotency_key") or [None])[0]
        state.calls.append((method, path, tenant if isinstance(tenant, str) else None, key))
        if path == "/outbox-events":
            if method != "GET":
                start_response("405 Method Not Allowed", [("Content-Type", "application/json")])
                return [b"{}"]
            if state.list_status == 403:
                start_response("403 Forbidden", [("Content-Type", "application/json")])
                return [b"{}"]
            if state.list_status == 404:
                start_response("404 Not Found", [("Content-Type", "application/json")])
                return [b"{}"]
            if state.list_status == 500:
                start_response("500 Internal Server Error", [("Content-Type", "application/json")])
                return [b"{}"]
            cursor = (query.get("cursor") or [None])[0]
            page = state.pages[0]
            if cursor and len(state.pages) > 1:
                page = state.pages[1]
            body = json.dumps(page, separators=(",", ":")).encode("utf-8")
            start_response(
                "200 OK",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        if path.endswith("/publish"):
            if method != "POST":
                start_response("405 Method Not Allowed", [("Content-Type", "application/json")])
                return [b"{}"]
            outbox_event_id = path.split("/")[-2]
            status = state.publish_status.get((tenant or "", outbox_event_id), 200)
            if status == 403:
                start_response("403 Forbidden", [("Content-Type", "application/json")])
                return [b"{}"]
            if status == 404:
                start_response("404 Not Found", [("Content-Type", "application/json")])
                return [b"{}"]
            state.published.append(outbox_event_id)
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b"{}"]
        if path == "/posting-receipts":
            lookup = (tenant or "", key or "")
            receipt = state.receipts.get(lookup)
            if receipt is None:
                start_response("404 Not Found", [("Content-Type", "application/json")])
                return [b"{}"]
            body = json.dumps(receipt, separators=(",", ":")).encode("utf-8")
            start_response(
                "200 OK",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        start_response("404 Not Found", [("Content-Type", "application/json")])
        return [b"{}"]

    httpd = make_server("127.0.0.1", 0, application)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def ais_base_url(httpd: Any) -> str:
    """Return the local fake-AIS origin."""
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def outbox_item(
    *,
    proposal_id: UUID,
    outbox_event_id: str | None = None,
    event_type_code: str = "posting_receipt",
) -> dict[str, object]:
    """Return one AIS outbox row using the pinned URN mapping."""
    return {
        "outbox_event_id": outbox_event_id or str(uuid4()),
        "event_type_code": event_type_code,
        "aggregate_reference": general_journal_aggregate_reference(proposal_id),
        "payload_reference": posting_receipt_payload_reference(proposal_id),
        "payload_hash": "sha256:" + ("a" * 64),
        "created_at": "2026-08-17T19:00:00Z",
    }


class ScriptedOutboxClient:
    """Deterministic AIS client for drain tests that do not need a live server."""

    def __init__(
        self,
        pages: list[AisOutboxPage | BaseException] | None = None,
        receipts: dict[tuple[str, str], AisLookupResult | BaseException] | None = None,
        publishes: dict[str, AisLookupResult | BaseException] | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self._receipts = receipts or {}
        self._publishes = publishes or {}
        self.outbox_calls: list[tuple[str, str, int, str | None]] = []
        self.receipt_calls: list[tuple[str, str]] = []
        self.publish_calls: list[tuple[str, str]] = []

    def list_outbox_events(
        self,
        tenant_reference: str,
        event_type_code: str = "posting_receipt",
        page_limit: int = 50,
        cursor: str | None = None,
    ) -> AisOutboxPage:
        """Return the next scripted outbox page."""
        self.outbox_calls.append((tenant_reference, event_type_code, page_limit, cursor))
        result = self._pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def get_posting_receipt(self, tenant_reference: str, idempotency_key: str) -> AisLookupResult:
        """Return a scripted receipt lookup."""
        self.receipt_calls.append((tenant_reference, idempotency_key))
        result = self._receipts[(tenant_reference, idempotency_key)]
        if isinstance(result, BaseException):
            raise result
        return result

    def publish_outbox_event(self, tenant_reference: str, outbox_event_id: str) -> AisLookupResult:
        """Return a scripted publish result."""
        self.publish_calls.append((tenant_reference, outbox_event_id))
        result = self._publishes.get(outbox_event_id, AisLookupResult(status_code=200, raw_body=b""))
        if isinstance(result, BaseException):
            raise result
        return result


class AisOutboxDrainTests(unittest.TestCase):
    """Verify the drain matches constructed URNs and reuses the #16 receipt GET."""

    def test_pinned_urns_are_constructed_from_proposal_id(self) -> None:
        """AIS Draft #2 pins payload and aggregate URNs to Billing proposal_id."""
        self.assertEqual(
            posting_receipt_payload_reference(EXAMPLE_PROPOSAL_ID),
            "urn:cwl:accounting:posting_receipt:11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(
            general_journal_aggregate_reference(EXAMPLE_PROPOSAL_ID),
            "urn:cwl:accounting:general_journal:11111111-1111-1111-1111-111111111111",
        )

    def test_empty_outbox_skips_receipt_polls(self) -> None:
        """An empty unpublished set is success and must not GET posting-receipts."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        client = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(), next_cursor=None)]
        )
        result = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(result.ais_outbox_drain_outcome_code, AisOutboxDrainOutcomeCode.ACCEPTED)
        self.assertEqual(result.outbox_event_count, 0)
        self.assertEqual(result.receipt_lookup_count, 0)
        self.assertEqual(result.published_event_count, 0)
        self.assertIsNone(result.next_cursor)
        self.assertEqual(client.receipt_calls, [])
        self.assertEqual(client.publish_calls, [])
        self.assertEqual(ar_proposal.proposal_status, "validated")
        self.assertEqual(validate_ais_outbox_drain(result.as_contract_dict()), ())

    def test_matched_proposal_pulls_idempotency_key_and_publishes(self) -> None:
        """A matched posting_receipt row uses the stored key, never the payload URN."""
        ledger, ar_proposal, cash_proposal = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        assert cash_proposal.proposal_id is not None
        state = FakeAisOutboxState()
        event_id = str(uuid4())
        state.pages = [{"outbox_events": [outbox_item(proposal_id=ar_proposal.proposal_id, outbox_event_id=event_id)], "next_cursor": None}]
        state.receipts[(TENANT_ONE, str(ar_proposal.idempotency_key))] = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(ar_proposal.idempotency_key),
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        httpd = start_fake_ais_outbox(state)
        try:
            client = AisPostingReceiptClient(ais_base_url(httpd))
            result = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox(TENANT_ONE)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(result.receipt_lookup_count, 1)
        self.assertEqual(result.published_event_count, 1)
        self.assertEqual(state.published, [event_id])
        receipt_queries = [call for call in state.calls if call[1] == "/posting-receipts"]
        self.assertEqual(len(receipt_queries), 1)
        self.assertEqual(receipt_queries[0][3], ar_proposal.idempotency_key)
        self.assertNotIn("urn:cwl:accounting:posting_receipt", str(receipt_queries[0][3]))
        stored = ledger.find_posting_receipt_observation(
            ledger.require_tenant(TENANT_ONE).tenant_account_id, str(ar_proposal.idempotency_key)
        )
        self.assertIsNotNone(stored)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")
        self.assertIn(":invoice_draft:", str(ar_proposal.idempotency_key))
        self.assertIn(":cash_receipt:", str(cash_proposal.idempotency_key))

    def test_existing_observation_publishes_without_a_second_receipt_get(self) -> None:
        """A stored observation is enough to publish the matched outbox id."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(ar_proposal.idempotency_key),
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        pull_client = ScriptedOutboxClient(
            receipts={
                (TENANT_ONE, str(ar_proposal.idempotency_key)): AisLookupResult(
                    status_code=200, raw_body=json.dumps(receipt).encode("utf-8")
                )
            }
        )
        PostingReceiptPullService(ledger, ais_client=pull_client).pull_posting_receipt(
            TENANT_ONE, str(ar_proposal.idempotency_key)
        )
        event = AisOutboxEvent(
            outbox_event_id=str(uuid4()),
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(ar_proposal.proposal_id),
            payload_reference=posting_receipt_payload_reference(ar_proposal.proposal_id),
            payload_hash="sha256:" + ("b" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        client = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(event,), next_cursor=None)]
        )
        result = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(result.receipt_lookup_count, 0)
        self.assertEqual(result.observed_receipt_count, 1)
        self.assertEqual(result.published_event_count, 1)
        self.assertEqual(client.receipt_calls, [])
        self.assertEqual(client.publish_calls, [(TENANT_ONE, event.outbox_event_id)])

    def test_unmatched_and_foreign_event_types_are_not_drained(self) -> None:
        """Do not parse payload_reference or drain journal_reversal / period_close."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        foreign = AisOutboxEvent(
            outbox_event_id=str(uuid4()),
            event_type_code="journal_reversal",
            aggregate_reference=general_journal_aggregate_reference(ar_proposal.proposal_id),
            payload_reference=posting_receipt_payload_reference(ar_proposal.proposal_id),
            payload_hash="sha256:" + ("c" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        period_close = replace(foreign, outbox_event_id=str(uuid4()), event_type_code="period_close")
        lookalike = AisOutboxEvent(
            outbox_event_id=str(uuid4()),
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(ar_proposal.proposal_id),
            payload_reference=str(ar_proposal.idempotency_key),
            payload_hash="sha256:" + ("d" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        other_uuid = AisOutboxEvent(
            outbox_event_id=str(uuid4()),
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(EXAMPLE_PROPOSAL_ID),
            payload_reference=posting_receipt_payload_reference(EXAMPLE_PROPOSAL_ID),
            payload_hash="sha256:" + ("e" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        client = ScriptedOutboxClient(
            pages=[
                AisOutboxPage(
                    status_code=200,
                    outbox_events=(foreign, period_close, lookalike, other_uuid),
                    next_cursor=None,
                )
            ]
        )
        result = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(result.outbox_event_count, 4)
        self.assertEqual(result.receipt_lookup_count, 0)
        self.assertEqual(result.published_event_count, 0)
        self.assertEqual(result.skipped_event_count, 4)
        self.assertEqual(client.receipt_calls, [])

    def test_items_envelope_is_ignored_and_counts_as_empty(self) -> None:
        """Body ``items`` is never a substitute for ``outbox_events``."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        opener_calls: list[str] = []

        def opener(request: Any, timeout: float | None = None) -> Any:
            opener_calls.append(str(request.full_url))
            raise AssertionError("items-only envelopes must fail before receipt GETs")

        client = AisPostingReceiptClient("http://127.0.0.1:9", urlopen=opener)
        with mock.patch.object(
            client,
            "list_outbox_events",
            return_value=AisOutboxPage(status_code=200, outbox_events=(), next_cursor=None),
        ):
            result = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(result.receipt_lookup_count, 0)
        self.assertEqual(opener_calls, [])

    def test_http_drain_and_fail_closed_edges(self) -> None:
        """POST /v1/ais-outbox-drains uses the tenant pin and #22 key rule."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        event = AisOutboxEvent(
            outbox_event_id=str(uuid4()),
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(ar_proposal.proposal_id),
            payload_reference=posting_receipt_payload_reference(ar_proposal.proposal_id),
            payload_hash="sha256:" + ("f" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(ar_proposal.idempotency_key),
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        client = ScriptedOutboxClient(
            pages=[
                AisOutboxPage(status_code=200, outbox_events=(event,), next_cursor=None),
                AisOutboxPage(status_code=200, outbox_events=(), next_cursor=None),
            ],
            receipts={
                (TENANT_ONE, str(ar_proposal.idempotency_key)): AisLookupResult(
                    status_code=200, raw_body=json.dumps(receipt).encode("utf-8")
                )
            },
        )
        app = create_http_app(ledger, ais_client=client)
        status, body = invoke_http(
            app,
            "POST",
            "/v1/ais-outbox-drains",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["published_event_count"], 1)
        self.assertEqual(body["receipt_lookup_count"], 1)
        self.assertNotIn("webhook_secret", json.dumps(body))
        self.assertNotIn("api_credential_secret", json.dumps(body))
        method_status, method_body = invoke_http(app, "PUT", "/v1/ais-outbox-drains")
        self.assertEqual(method_status, 422)
        missing = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox("")
        self.assertEqual(missing.rejection_reason_code, AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND)
        unknown = AisOutboxDrainService(ledger, ais_client=client).drain_ais_outbox("urn:cwl:missing")
        self.assertEqual(unknown.rejection_reason_code, AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND)
        unconfigured = AisOutboxDrainService(ledger).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(
            unconfigured.rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED,
        )
        insecure = AisOutboxDrainService(
            ledger, ais_client=AisPostingReceiptClient("http://ais.example")
        ).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(
            insecure.rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.AIS_BASE_URL_INSECURE,
        )
        file_url = AisOutboxDrainService(
            ledger, ais_client=AisPostingReceiptClient("file:///tmp/ais")
        ).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(
            file_url.rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.AIS_BASE_URL_INSECURE,
        )
        self.assertTrue(ais_base_url_is_allowed("https://ais.example"))
        self.assertTrue(ais_base_url_is_allowed("http://localhost:8080"))
        self.assertTrue(ais_base_url_is_allowed("http://127.0.0.1:9"))
        self.assertTrue(ais_base_url_is_allowed("http://[::1]/ais"))
        self.assertFalse(ais_base_url_is_allowed("https://"))
        self.assertFalse(ais_base_url_is_allowed(""))
        self.assertFalse(ais_base_url_is_allowed("ftp://ais.example"))
        missing_status, missing_body = invoke_http(create_http_app(ledger), "POST", "/v1/ais-outbox-drains")
        self.assertEqual(missing_status, 422)
        with mock.patch(
            "metering_billing.http_app.AisOutboxDrainService.drain_ais_outbox",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger, ais_client=client),
                "POST",
                "/v1/ais-outbox-drains",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")

    def test_client_and_drain_coverage_edges(self) -> None:
        """Cover transport, pagination, publish failures, and cash/credit keys."""
        ledger, ar_proposal, cash_proposal = persist_known_ar_and_cash_proposals()
        assert ar_proposal.proposal_id is not None
        assert cash_proposal.proposal_id is not None
        first_event = AisOutboxEvent(
            outbox_event_id="outbox-one",
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(ar_proposal.proposal_id),
            payload_reference=posting_receipt_payload_reference(ar_proposal.proposal_id),
            payload_hash="sha256:" + ("1" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        second_event = AisOutboxEvent(
            outbox_event_id="outbox-two",
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(cash_proposal.proposal_id),
            payload_reference=posting_receipt_payload_reference(cash_proposal.proposal_id),
            payload_hash="sha256:" + ("2" * 64),
            created_at="2026-08-17T19:01:00Z",
        )
        ar_receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(ar_proposal.idempotency_key),
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        cash_receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(cash_proposal.idempotency_key),
            source_proposal_id=str(cash_proposal.proposal_id),
            source_payload_hash=str(cash_proposal.source_payload_hash),
        )
        paged = ScriptedOutboxClient(
            pages=[
                AisOutboxPage(
                    status_code=200,
                    outbox_events=(first_event,),
                    next_cursor="2026-08-17T19:00:00Z|outbox-one",
                ),
                AisOutboxPage(status_code=200, outbox_events=(second_event,), next_cursor=None),
            ],
            receipts={
                (TENANT_ONE, str(ar_proposal.idempotency_key)): AisLookupResult(
                    status_code=200, raw_body=json.dumps(ar_receipt).encode("utf-8")
                ),
                (TENANT_ONE, str(cash_proposal.idempotency_key)): AisLookupResult(
                    status_code=200, raw_body=json.dumps(cash_receipt).encode("utf-8")
                ),
            },
        )
        paged_result = AisOutboxDrainService(ledger, ais_client=paged).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(paged_result.published_event_count, 2)
        self.assertEqual(paged_result.receipt_lookup_count, 2)
        self.assertEqual(len(paged.outbox_calls), 2)
        self.assertEqual(paged.outbox_calls[1][3], "2026-08-17T19:00:00Z|outbox-one")
        self.assertIn(":cash_receipt:", paged.receipt_calls[1][1])
        credit_ledger, invoice_draft_id = draft_known_morning()
        credit = CreditAdjustmentService(credit_ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, PARTIAL_CREDIT_AMOUNT, "goodwill"
        )
        assert credit.proposal_id is not None
        credit_proposal = credit_ledger.get_journal_proposal(credit.proposal_id)
        assert credit_proposal is not None
        credit_event = AisOutboxEvent(
            outbox_event_id="outbox-credit",
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(credit.proposal_id),
            payload_reference=posting_receipt_payload_reference(credit.proposal_id),
            payload_hash="sha256:" + ("3" * 64),
            created_at="2026-08-17T19:02:00Z",
        )
        credit_receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=credit_proposal.idempotency_key,
            source_proposal_id=str(credit.proposal_id),
            source_payload_hash=credit_proposal.source_payload_hash,
        )
        credit_client = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(credit_event,), next_cursor=None)],
            receipts={
                (TENANT_ONE, credit_proposal.idempotency_key): AisLookupResult(
                    status_code=200, raw_body=json.dumps(credit_receipt).encode("utf-8")
                )
            },
        )
        credit_result = AisOutboxDrainService(credit_ledger, ais_client=credit_client).drain_ais_outbox(
            TENANT_ONE
        )
        self.assertEqual(credit_result.published_event_count, 1)
        self.assertIn(":credit_adjustment:", credit_client.receipt_calls[0][1])
        forbidden = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=403, outbox_events=(), next_cursor=None)]
        )
        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=forbidden).drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.CROSS_TENANT,
        )
        missing_box = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=404, outbox_events=(), next_cursor=None)]
        )
        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=missing_box).drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.AIS_OUTBOX_INVALID,
        )
        unexpected = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=201, outbox_events=(), next_cursor=None)]
        )
        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=unexpected).drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.TRANSPORT_FAILURE,
        )
        transport = ScriptedOutboxClient(pages=[AisTransportError("transport_failure")])
        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=transport).drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.TRANSPORT_FAILURE,
        )
        class ReceiptOnly:
            def get_posting_receipt(self, tenant_reference: str, idempotency_key: str) -> AisLookupResult:
                raise AssertionError("drain must not fall back to receipt-only clients")

        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=ReceiptOnly()).drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED,
        )
        not_ready = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(first_event,), next_cursor=None)],
            receipts={
                (TENANT_ONE, str(ar_proposal.idempotency_key)): AisLookupResult(status_code=404, raw_body=b"")
            },
        )
        not_ready_ledger, not_ready_ar, _not_ready_cash = persist_known_ar_and_cash_proposals()
        not_ready_event = AisOutboxEvent(
            outbox_event_id="outbox-late",
            event_type_code="posting_receipt",
            aggregate_reference=general_journal_aggregate_reference(not_ready_ar.proposal_id),
            payload_reference=posting_receipt_payload_reference(not_ready_ar.proposal_id),
            payload_hash="sha256:" + ("4" * 64),
            created_at="2026-08-17T19:00:00Z",
        )
        not_ready._pages = [AisOutboxPage(status_code=200, outbox_events=(not_ready_event,), next_cursor=None)]
        not_ready._receipts = {
            (TENANT_ONE, str(not_ready_ar.idempotency_key)): AisLookupResult(status_code=404, raw_body=b"")
        }
        late = AisOutboxDrainService(not_ready_ledger, ais_client=not_ready).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(late.published_event_count, 0)
        self.assertEqual(late.skipped_event_count, 1)
        self.assertEqual(not_ready.publish_calls, [])
        publish_forbidden = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(first_event,), next_cursor=None)],
            receipts={
                (TENANT_ONE, str(ar_proposal.idempotency_key)): AisLookupResult(
                    status_code=200, raw_body=json.dumps(ar_receipt).encode("utf-8")
                )
            },
            publishes={"outbox-one": AisLookupResult(status_code=403, raw_body=b"")},
        )
        forbidden_pub = AisOutboxDrainService(MemoryUsageLedger(), ais_client=publish_forbidden)
        forbidden_pub.ledger = ledger
        forbidden_pub.pulls = PostingReceiptPullService(ledger, ais_client=publish_forbidden)
        already = AisOutboxDrainService(ledger, ais_client=publish_forbidden).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(already.observed_receipt_count, 1)
        self.assertEqual(already.published_event_count, 0)
        publish_missing = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(second_event,), next_cursor=None)],
            publishes={"outbox-two": AisLookupResult(status_code=404, raw_body=b"")},
        )
        missing_pub = AisOutboxDrainService(ledger, ais_client=publish_missing).drain_ais_outbox(TENANT_ONE)
        self.assertEqual(missing_pub.published_event_count, 0)
        publish_error = ScriptedOutboxClient(
            pages=[AisOutboxPage(status_code=200, outbox_events=(second_event,), next_cursor=None)],
            publishes={"outbox-two": AisTransportError("transport_failure")},
        )
        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=publish_error).drain_ais_outbox(TENANT_ONE).published_event_count,
            0,
        )
        class PublishMissing:
            def list_outbox_events(self, *args: object, **kwargs: object) -> AisOutboxPage:
                return AisOutboxPage(status_code=200, outbox_events=(second_event,), next_cursor=None)

            def get_posting_receipt(self, tenant_reference: str, idempotency_key: str) -> AisLookupResult:
                return AisLookupResult(status_code=200, raw_body=json.dumps(cash_receipt).encode("utf-8"))

        self.assertEqual(
            AisOutboxDrainService(ledger, ais_client=PublishMissing()).drain_ais_outbox(TENANT_ONE).published_event_count,
            0,
        )
        rejected = AisOutboxDrainResult(
            ais_outbox_drain_outcome_code=AisOutboxDrainOutcomeCode.REJECTED,
            ais_outbox_drain_contract_version=1,
            outbox_event_count=0,
            receipt_lookup_count=0,
            observed_receipt_count=0,
            published_event_count=0,
            skipped_event_count=0,
            next_cursor=None,
            rejection_reason_code=None,
        )
        self.assertEqual(rejected.as_contract_dict()["rejection_reason_code"], "tenant_not_found")
        with self.assertRaises(ValueError):
            replace(rejected, ais_outbox_drain_outcome_code="posted").as_contract_dict()  # type: ignore[arg-type]
        default_service = AisOutboxDrainService()
        self.assertEqual(
            default_service.drain_ais_outbox(TENANT_ONE).rejection_reason_code,
            AisOutboxDrainRejectionReasonCode.TENANT_NOT_FOUND,
        )
        captured: dict[str, str] = {}

        class _Ok:
            status = 200

            def read(self) -> bytes:
                return b'{"outbox_events":[],"next_cursor":null}'

            def __enter__(self) -> _Ok:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def capture_open(request: Any, timeout: float | None = None) -> _Ok:
            captured["url"] = str(request.full_url)
            captured["method"] = request.get_method()
            return _Ok()

        listed = AisPostingReceiptClient("http://127.0.0.1:9", urlopen=capture_open).list_outbox_events(
            TENANT_ONE, cursor="2026-08-17T19:00:00Z|abc"
        )
        self.assertEqual(listed.outbox_events, ())
        self.assertIn("event_type_code=posting_receipt", captured["url"])
        self.assertIn("page_limit=50", captured["url"])
        self.assertIn("cursor=", captured["url"])
        self.assertNotIn("items", captured["url"])
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").list_outbox_events(TENANT_ONE, event_type_code="journal_reversal")
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").list_outbox_events(TENANT_ONE, page_limit=0)
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").list_outbox_events(TENANT_ONE, page_limit=101)
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").list_outbox_events(TENANT_ONE, page_limit=True)  # type: ignore[arg-type]
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").list_outbox_events(TENANT_ONE, cursor="")
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9").publish_outbox_event(TENANT_ONE, "")

        def raise_forbidden(request: Any, timeout: float | None = None) -> Any:
            raise HTTPError(str(request.full_url), 403, "forbidden", None, None)

        def raise_missing(request: Any, timeout: float | None = None) -> Any:
            raise HTTPError(str(request.full_url), 404, "missing", None, None)

        def raise_server(request: Any, timeout: float | None = None) -> Any:
            raise HTTPError(str(request.full_url), 500, "error", None, None)

        def raise_url(request: Any, timeout: float | None = None) -> Any:
            raise URLError("closed")

        self.assertEqual(
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_forbidden).list_outbox_events(TENANT_ONE).status_code,
            403,
        )
        self.assertEqual(
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_missing).list_outbox_events(TENANT_ONE).status_code,
            404,
        )
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_server).list_outbox_events(TENANT_ONE)
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_url).list_outbox_events(TENANT_ONE)
        self.assertEqual(
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_forbidden).publish_outbox_event(TENANT_ONE, "x").status_code,
            403,
        )
        self.assertEqual(
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_missing).publish_outbox_event(TENANT_ONE, "x").status_code,
            404,
        )
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_server).publish_outbox_event(TENANT_ONE, "x")
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=raise_url).publish_outbox_event(TENANT_ONE, "x")

        class _Statusless:
            def read(self) -> bytes:
                return b'{"outbox_events":[],"next_cursor":null}'

            def __enter__(self) -> _Statusless:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        statusless = AisPostingReceiptClient("http://127.0.0.1:9", urlopen=lambda request, timeout=None: _Statusless())
        self.assertEqual(statusless.list_outbox_events(TENANT_ONE).status_code, 200)
        self.assertEqual(statusless.publish_outbox_event(TENANT_ONE, "x").status_code, 200)

        class _Created:
            status = 201

            def read(self) -> bytes:
                return b"{}"

            def __enter__(self) -> _Created:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=lambda request, timeout=None: _Created()).list_outbox_events(
                TENANT_ONE
            )
        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=lambda request, timeout=None: _Created()).publish_outbox_event(
                TENANT_ONE, "x"
            )

        class _NoContent:
            status = 204

            def read(self) -> bytes:
                return b""

            def __enter__(self) -> _NoContent:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        self.assertEqual(
            AisPostingReceiptClient("http://127.0.0.1:9", urlopen=lambda request, timeout=None: _NoContent()).publish_outbox_event(
                TENANT_ONE, "x"
            ).status_code,
            204,
        )
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b"{")
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b"[]")
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b'{"items":[],"cursor":null}')
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b'{"outbox_events":null,"next_cursor":null}')
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b'{"outbox_events":[],"next_cursor":1}')
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b'{"outbox_events":[1],"next_cursor":null}')
        with self.assertRaises(AisTransportError):
            _parse_outbox_page(b'{"outbox_events":[{}],"next_cursor":null}')
        parsed = _parse_outbox_page(
            json.dumps(
                {
                    "outbox_events": [outbox_item(proposal_id=EXAMPLE_PROPOSAL_ID)],
                    "next_cursor": None,
                    "items": [{"should": "ignore"}],
                    "cursor": "do-not-read",
                }
            ).encode("utf-8")
        )
        self.assertEqual(len(parsed.outbox_events), 1)
        self.assertIsNone(parsed.next_cursor)
        header_status, header_body = invoke_http(
            create_http_app(ledger, ais_client=ScriptedOutboxClient(pages=[AisOutboxPage(status_code=200, outbox_events=(), next_cursor=None)])),
            "POST",
            "/v1/ais-outbox-drains",
            {},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_status, 200)
        self.assertEqual(header_body["outbox_event_count"], 0)
        mismatch_status, mismatch_body = invoke_http(
            create_http_app(ledger),
            "POST",
            "/v1/ais-outbox-drains",
            {"tenant_reference": TENANT_TWO},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(mismatch_status, 422)
        get_publish_state = FakeAisOutboxState()
        get_publish_httpd = start_fake_ais_outbox(get_publish_state)
        try:
            publish_url = f"{ais_base_url(get_publish_httpd)}/outbox-events/{uuid4()}/publish"
            try:
                urlopen(Request(publish_url, method="GET"), timeout=2)
                self.fail("GET publish must be 405")
            except HTTPError as error:
                self.assertEqual(error.code, 405)
            try:
                urlopen(Request(f"{ais_base_url(get_publish_httpd)}/outbox-events", data=b"{}", method="POST"), timeout=2)
                self.fail("POST /outbox-events must be 405")
            except HTTPError as error:
                self.assertEqual(error.code, 405)
        finally:
            get_publish_httpd.shutdown()
            get_publish_httpd.server_close()
