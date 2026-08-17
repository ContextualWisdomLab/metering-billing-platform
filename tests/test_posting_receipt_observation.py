"""Posting-receipt observation tests: AIS pull, replay, and no status flip."""

from __future__ import annotations

import io
import json
import threading
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote
from uuid import uuid4
from wsgiref.simple_server import make_server

from metering_billing import (
    AccountingExportService,
    MemoryUsageLedger,
    PostingReceiptPullService,
    create_http_app,
)
from metering_billing.contracts import (
    default_consumed_schemas_directory,
    validate_consumed_posting_receipt,
)
from metering_billing.errors import (
    PostingReceiptObservationOutcomeCode,
    PostingReceiptObservationQueryError,
    PostingReceiptObservationRejectionReasonCode,
)
from metering_billing.posting_receipt import (
    AisLookupResult,
    AisPostingReceiptClient,
    AisTransportError,
    PostingReceiptObservationResult,
)
from metering_billing.usage_ledger import StoredPostingReceiptObservation, generate_record_id
from test_cash_journal_proposal import record_known_morning_receipt
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


def persist_known_ar_and_cash_proposals() -> tuple[MemoryUsageLedger, Any, Any]:
    """Persist the published invoice and cash journal proposals for TENANT_ONE."""
    ledger, payment_receipt_id, collection_case_id = record_known_morning_receipt()
    invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
    exports = AccountingExportService(ledger)
    ar_proposal = exports.propose_journal(TENANT_ONE, invoice_draft_id)
    cash_proposal = exports.propose_cash_journal(TENANT_ONE, payment_receipt_id)
    return ledger, ar_proposal, cash_proposal


def make_ais_receipt(
    *,
    tenant_reference: str,
    idempotency_key: str,
    source_proposal_id: str,
    source_payload_hash: str,
    posting_status_code: str = "posted",
    receipt_id: str | None = None,
    **optional_fields: object,
) -> dict[str, object]:
    """Return one AIS-owned posting receipt using the published field set."""
    receipt: dict[str, object] = {
        "receipt_id": receipt_id or str(uuid4()),
        "receipt_contract_version": 1,
        "idempotency_key": idempotency_key,
        "source_proposal_id": str(source_proposal_id),
        "source_payload_hash": source_payload_hash,
        "tenant_reference": tenant_reference,
        "legal_entity_reference": "urn:cwl:entity_001",
        "accounting_book_reference": "urn:cwl:book_primary",
        "accounting_policy_version": "policy-2026.1",
        "posting_rule_version": "rule-2026.1",
        "posting_status_code": posting_status_code,
        "recorded_at": "2026-08-17T18:00:00Z",
    }
    receipt.update(optional_fields)
    return receipt


class FakeAisState:
    """In-process AIS lookup table keyed by tenant pin and idempotency key."""

    def __init__(self) -> None:
        self.receipts: dict[tuple[str, str], dict[str, object]] = {}
        self.raw_bodies: dict[tuple[str, str], bytes] = {}
        self.status_overrides: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, str | None, str | None]] = []


def start_fake_ais(state: FakeAisState) -> Any:
    """Serve GET /posting-receipts on a local stdlib HTTP server."""

    def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        tenant = environ.get("HTTP_X_CWL_TENANT_REFERENCE")
        if not isinstance(tenant, str):
            tenant = None
        query = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        key_values = query.get("idempotency_key") or [None]
        key = key_values[0]
        path = str(environ.get("PATH_INFO") or "")
        state.calls.append((path, tenant, key))
        lookup = (tenant or "", key or "")
        if path != "/posting-receipts":
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b"{}"]
        override = state.status_overrides.get(lookup)
        if override == 403:
            start_response("403 Forbidden", [("Content-Type", "application/json")])
            return [b"{}"]
        if override == 500:
            start_response("500 Internal Server Error", [("Content-Type", "application/json")])
            return [b"{}"]
        if override == 404 or (
            lookup not in state.receipts and lookup not in state.raw_bodies
        ):
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b"{}"]
        if lookup in state.raw_bodies:
            body = state.raw_bodies[lookup]
        else:
            body = json.dumps(state.receipts[lookup], separators=(",", ":")).encode("utf-8")
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    httpd = make_server("127.0.0.1", 0, application)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def ais_base_url(httpd: Any) -> str:
    """Return the local fake-AIS origin."""
    return f"http://127.0.0.1:{httpd.server_address[1]}"


class ScriptedAisClient:
    """Deterministic AIS client used when a live HTTP server is unnecessary."""

    def __init__(self, results: list[AisLookupResult | BaseException]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def get_posting_receipt(self, tenant_reference: str, idempotency_key: str) -> AisLookupResult:
        """Return the next scripted lookup or raise the next scripted error."""
        self.calls.append((tenant_reference, idempotency_key))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _lookup_result(status_code: int, payload: object | None = None) -> AisLookupResult:
    """Encode a scripted AIS HTTP result."""
    raw_body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return AisLookupResult(status_code=status_code, raw_body=raw_body)


class PostingReceiptObservationTests(unittest.TestCase):
    """Verify AIS receipts become observations without flipping proposal_status."""

    def test_consumed_schema_is_ais_owned_and_not_a_billing_contract(self) -> None:
        """The copied schema stays a consumer contract under schemas/consumed/."""
        schema_path = (
            default_consumed_schemas_directory() / "accounting-posting-receipt.schema.json"
        )
        self.assertTrue(schema_path.is_file())
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["x-cwl-authority"], "accounting-information-platform")
        self.assertEqual(
            schema["$id"],
            "https://schemas.contextualwisdomlab.org/accounting/posting-receipt/v1",
        )
        self.assertEqual(
            schema["properties"]["posting_status_code"]["enum"],
            ["posted", "held", "rejected", "reversed"],
        )
        billing_owned = {
            path.name
            for path in schema_path.parents[1].glob("*.schema.json")
        }
        self.assertNotIn("accounting-posting-receipt.schema.json", billing_owned)

    def test_fake_ais_posted_receipts_store_replay_and_leave_proposals_validated(self) -> None:
        """Published invoice and cash keys store observations; replay is identical."""
        ledger, ar_proposal, cash_proposal = persist_known_ar_and_cash_proposals()
        self.assertEqual(ar_proposal.proposal_status, "validated")
        self.assertEqual(cash_proposal.proposal_status, "validated")
        state = FakeAisState()
        ar_receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(ar_proposal.idempotency_key),
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
            journal_reference="urn:cwl:journal_ar",
            fiscal_period_reference="urn:cwl:period_2026_08",
            posted_at="2026-08-17T18:05:00Z",
            line_count=2,
            transaction_currency="USD",
            functional_currency="USD",
        )
        cash_receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=str(cash_proposal.idempotency_key),
            source_proposal_id=str(cash_proposal.proposal_id),
            source_payload_hash=str(cash_proposal.source_payload_hash),
            journal_reference="urn:cwl:journal_cash",
            posted_at="2026-08-17T18:06:00Z",
            line_count=2,
            transaction_currency="USD",
            functional_currency="USD",
        )
        state.receipts[(TENANT_ONE, str(ar_proposal.idempotency_key))] = ar_receipt
        state.receipts[(TENANT_ONE, str(cash_proposal.idempotency_key))] = cash_receipt
        httpd = start_fake_ais(state)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        service = PostingReceiptPullService(
            ledger, ais_client=AisPostingReceiptClient(ais_base_url(httpd))
        )

        first_ar = service.pull_posting_receipt(TENANT_ONE, str(ar_proposal.idempotency_key))
        first_cash = service.pull_posting_receipt(TENANT_ONE, str(cash_proposal.idempotency_key))
        replay_ar = service.pull_posting_receipt(TENANT_ONE, str(ar_proposal.idempotency_key))

        self.assertEqual(
            first_ar.posting_receipt_observation_outcome_code,
            PostingReceiptObservationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            first_cash.posting_receipt_observation_outcome_code,
            PostingReceiptObservationOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            replay_ar.posting_receipt_observation_outcome_code,
            PostingReceiptObservationOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(replay_ar.posting_receipt_observation_id, first_ar.posting_receipt_observation_id)
        self.assertEqual(first_ar.posting_status_code, "posted")
        self.assertEqual(first_cash.posting_status_code, "posted")
        self.assertEqual(validate_consumed_posting_receipt(ar_receipt), ())
        self.assertEqual(
            validate_consumed_posting_receipt(ar_receipt, default_consumed_schemas_directory()),
            (),
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 2)
        stored_ar = ledger.get_journal_proposal(ar_proposal.proposal_id)
        stored_cash = ledger.get_journal_proposal(cash_proposal.proposal_id)
        assert stored_ar is not None
        assert stored_cash is not None
        self.assertEqual(stored_ar.proposal_status, "validated")
        self.assertEqual(stored_cash.proposal_status, "validated")
        self.assertEqual(len(state.calls), 3)
        self.assertEqual(state.calls[0][0], "/posting-receipts")
        self.assertEqual(state.calls[0][1], TENANT_ONE)

    def test_ais_403_and_404_write_zero_rows(self) -> None:
        """Cross-tenant 403 and not-yet-accepted 404 must not invent a receipt."""
        ledger, ar_proposal, _cash_proposal = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        state = FakeAisState()
        state.status_overrides[(TENANT_ONE, key)] = 403
        httpd = start_fake_ais(state)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        service = PostingReceiptPullService(
            ledger, ais_client=AisPostingReceiptClient(ais_base_url(httpd))
        )

        forbidden = service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            forbidden.posting_receipt_observation_outcome_code,
            PostingReceiptObservationOutcomeCode.REJECTED,
        )
        self.assertEqual(
            forbidden.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.CROSS_TENANT,
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 0)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")

        state.status_overrides[(TENANT_ONE, key)] = 404
        missing = service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            missing.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.NOT_YET_ACCEPTED,
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 0)
        self.assertIsNone(missing.posting_receipt_observation_id)

    def test_held_rejected_reversed_store_without_flipping_proposal_status(self) -> None:
        """AIS-owned held, rejected, and reversed outcomes stay observations only."""
        ledger, ar_proposal, cash_proposal = persist_known_ar_and_cash_proposals()
        extras = (
            (
                str(ar_proposal.idempotency_key),
                str(ar_proposal.proposal_id),
                str(ar_proposal.source_payload_hash),
                "held",
                {"hold_reason_code": "period_closed"},
            ),
            (
                str(cash_proposal.idempotency_key),
                str(cash_proposal.proposal_id),
                str(cash_proposal.source_payload_hash),
                "rejected",
                {"rejection_reason_code": "unbalanced_journal"},
            ),
        )
        state = FakeAisState()
        for key, proposal_id, payload_hash, status_code, optional in extras:
            state.receipts[(TENANT_ONE, key)] = make_ais_receipt(
                tenant_reference=TENANT_ONE,
                idempotency_key=key,
                source_proposal_id=proposal_id,
                source_payload_hash=payload_hash,
                posting_status_code=status_code,
                **optional,
            )
        reversed_key = f"{TENANT_ONE}:reversed:{uuid4()}"
        state.receipts[(TENANT_ONE, reversed_key)] = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=reversed_key,
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
            posting_status_code="reversed",
            reversal_of_journal_reference="urn:cwl:journal_original",
        )
        httpd = start_fake_ais(state)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        service = PostingReceiptPullService(
            ledger, ais_client=AisPostingReceiptClient(ais_base_url(httpd))
        )

        held = service.pull_posting_receipt(TENANT_ONE, extras[0][0])
        rejected = service.pull_posting_receipt(TENANT_ONE, extras[1][0])
        reversed_result = service.pull_posting_receipt(TENANT_ONE, reversed_key)
        self.assertEqual(held.posting_status_code, "held")
        self.assertEqual(rejected.posting_status_code, "rejected")
        self.assertEqual(reversed_result.posting_status_code, "reversed")
        self.assertEqual(held.posting_receipt_observation_outcome_code, PostingReceiptObservationOutcomeCode.ACCEPTED)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")
        self.assertEqual(ledger.get_journal_proposal(cash_proposal.proposal_id).proposal_status, "validated")
        self.assertEqual(len(ledger.posting_receipt_observations), 3)

    def test_fail_closed_inputs_and_conflicting_receipt(self) -> None:
        """Missing tenant/key, illegal status, tenant mismatch, floats, and conflicts fail closed."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        valid = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=key,
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        service = PostingReceiptPullService(ledger, ais_client=ScriptedAisClient([]))
        missing_tenant = service.pull_posting_receipt("", key)
        missing_key = service.pull_posting_receipt(TENANT_ONE, "")
        unknown_tenant = service.pull_posting_receipt("urn:cwl:missing_tenant", key)
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(
            missing_key.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.IDEMPOTENCY_KEY_MISSING,
        )
        self.assertEqual(
            unknown_tenant.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 0)

        unconfigured = PostingReceiptPullService(ledger).pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            unconfigured.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.AIS_ENDPOINT_UNCONFIGURED,
        )

        illegal = dict(valid, posting_status_code="posted_maybe")
        illegal_service = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(200, illegal)])
        )
        illegal_result = illegal_service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            illegal_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        mismatched = dict(valid, tenant_reference=TENANT_TWO)
        mismatch_service = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(200, mismatched)])
        )
        mismatch_result = mismatch_service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            mismatch_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.TENANT_MISMATCH,
        )

        float_service = PostingReceiptPullService(
            ledger,
            ais_client=ScriptedAisClient(
                [AisLookupResult(status_code=200, raw_body=b'{"receipt_id":1.5}')]
            ),
        )
        float_result = float_service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            float_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        unknown_json = PostingReceiptPullService(
            ledger,
            ais_client=ScriptedAisClient(
                [AisLookupResult(status_code=200, raw_body=b'["not-an-object"]')]
            ),
        )
        unknown_result = unknown_json.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            unknown_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        unreadable = PostingReceiptPullService(
            ledger,
            ais_client=ScriptedAisClient([AisLookupResult(status_code=200, raw_body=b"not-json")]),
        )
        unreadable_result = unreadable.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            unreadable_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        boolean_json = PostingReceiptPullService(
            ledger,
            ais_client=ScriptedAisClient([AisLookupResult(status_code=200, raw_body=b'{"ok":true}')]),
        )
        boolean_result = boolean_json.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            boolean_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        undecodable = PostingReceiptPullService(
            ledger,
            ais_client=ScriptedAisClient([AisLookupResult(status_code=200, raw_body=b"\xff\xfe")]),
        )
        undecodable_result = undecodable.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            undecodable_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        wrong_key = dict(valid, idempotency_key="different-key")
        wrong_key_result = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(200, wrong_key)])
        ).pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            wrong_key_result.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.RECEIPT_INVALID,
        )

        unexpected_status = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(500)])
        ).pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            unexpected_status.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.TRANSPORT_FAILURE,
        )

        accept_client = ScriptedAisClient([_lookup_result(200, valid), _lookup_result(200, valid)])
        accept_service = PostingReceiptPullService(ledger, ais_client=accept_client)
        accepted = accept_service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(accepted.posting_receipt_observation_outcome_code, PostingReceiptObservationOutcomeCode.ACCEPTED)
        conflict_receipt = dict(valid, receipt_id=str(uuid4()))
        conflict_service = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(200, conflict_receipt)])
        )
        conflict = conflict_service.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            conflict.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT,
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 1)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")

    def test_transport_failure_and_client_http_errors(self) -> None:
        """Stdlib transport failures and unexpected AIS HTTP statuses fail closed."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        transport = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([AisTransportError("transport_failure")])
        )
        failed = transport.pull_posting_receipt(TENANT_ONE, key)
        self.assertEqual(
            failed.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.TRANSPORT_FAILURE,
        )
        self.assertEqual(len(ledger.posting_receipt_observations), 0)

        def raise_http(request: object, timeout: object = None) -> object:
            del request, timeout
            raise HTTPError("http://ais.test/posting-receipts", 500, "error", None, io.BytesIO(b""))

        def raise_url(request: object, timeout: object = None) -> object:
            del request, timeout
            raise URLError("connection refused")

        def raise_timeout(request: object, timeout: object = None) -> object:
            del request, timeout
            raise TimeoutError("timed out")

        def raise_os(request: object, timeout: object = None) -> object:
            del request, timeout
            raise OSError("reset")

        for opener in (raise_http, raise_url, raise_timeout, raise_os):
            client = AisPostingReceiptClient("http://ais.test", urlopen=opener)
            with self.assertRaises(AisTransportError):
                client.get_posting_receipt(TENANT_ONE, key)

        def raise_forbidden(request: object, timeout: object = None) -> object:
            del request, timeout
            raise HTTPError("http://ais.test/posting-receipts", 403, "no", None, io.BytesIO(b""))

        def raise_missing(request: object, timeout: object = None) -> object:
            del request, timeout
            raise HTTPError("http://ais.test/posting-receipts", 404, "no", None, io.BytesIO(b""))

        forbidden = AisPostingReceiptClient("http://ais.test", urlopen=raise_forbidden).get_posting_receipt(
            TENANT_ONE, key
        )
        missing = AisPostingReceiptClient("http://ais.test", urlopen=raise_missing).get_posting_receipt(
            TENANT_ONE, key
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(missing.status_code, 404)

        captured: dict[str, object] = {}

        class FakeResponse:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode("utf-8")

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> bool:
                del args
                return False

        def capture_open(request: object, timeout: object = None) -> FakeResponse:
            captured["url"] = getattr(request, "full_url", None)
            captured["headers"] = dict(getattr(request, "headers", {}))
            captured["timeout"] = timeout
            return FakeResponse()

        opened = AisPostingReceiptClient("http://ais.test/", urlopen=capture_open).get_posting_receipt(
            TENANT_ONE, key
        )
        self.assertEqual(opened.status_code, 200)
        self.assertIn("idempotency_key=", str(captured["url"]))
        headers = {str(name).lower(): value for name, value in dict(captured["headers"]).items()}
        self.assertEqual(headers.get("x-cwl-tenant-reference") or headers.get("X-Cwl-Tenant-Reference"), TENANT_ONE)

        class StatuslessResponse:
            def read(self) -> bytes:
                return b"{}"

            def __enter__(self) -> StatuslessResponse:
                return self

            def __exit__(self, *args: object) -> bool:
                del args
                return False

        statusless = AisPostingReceiptClient(
            "http://ais.test", urlopen=lambda request, timeout=None: StatuslessResponse()
        ).get_posting_receipt(TENANT_ONE, key)
        self.assertEqual(statusless.status_code, 200)

        def unexpected_status(request: object, timeout: object = None) -> FakeResponse:
            del request, timeout

            class Unexpected:
                status = 204

                def read(self) -> bytes:
                    return b""

                def __enter__(self) -> Unexpected:
                    return self

                def __exit__(self, *args: object) -> bool:
                    del args
                    return False

            return Unexpected()

        with self.assertRaises(AisTransportError):
            AisPostingReceiptClient("http://ais.test", urlopen=unexpected_status).get_posting_receipt(
                TENANT_ONE, key
            )

    def test_http_post_and_get_observation_without_calling_ais_on_get(self) -> None:
        """Operators POST a pull; GET reads the stored observation only."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=key,
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
            posted_at="2026-08-17T18:05:00Z",
            line_count=2,
            transaction_currency="USD",
            functional_currency="USD",
        )
        client = ScriptedAisClient([_lookup_result(200, receipt), _lookup_result(200, receipt)])
        app = create_http_app(ledger, ais_client=client)

        status, body = invoke_http(
            app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["posting_receipt_observation_outcome_code"], "accepted")
        self.assertEqual(body["posting_status_code"], "posted")
        self.assertNotIn("proposal_status", body)
        self.assertEqual(body["idempotency_key"], key)
        observation_id = body["posting_receipt_observation_id"]

        replay_status, replay_body = invoke_http(
            app,
            "POST",
            "/v1/posting-receipt-observations",
            {"idempotency_key": key},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["posting_receipt_observation_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["posting_receipt_observation_id"], observation_id)

        raising_client = ScriptedAisClient([AisTransportError("should_not_run")])
        ledger.register_tenant(TENANT_TWO)
        read_app = create_http_app(ledger, ais_client=raising_client)
        get_status, get_body = invoke_http(
            read_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(key, safe='')}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["posting_receipt_observation_id"], observation_id)
        self.assertEqual(get_body["posting_status_code"], "posted")
        self.assertEqual(raising_client.calls, [])
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")

        other_status, other_body = invoke_http(
            read_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(key, safe='')}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "observation_not_found")

        missing_status, missing_body = invoke_http(
            read_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote('missing-key', safe='')}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_body["rejection_reason_code"], "observation_not_found")

    def test_http_post_rejects_mismatch_unconfigured_and_ais_errors(self) -> None:
        """HTTP pull uses the same tenant-pin rule and fails closed."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        empty_app = create_http_app(ledger)
        unconfigured_status, unconfigured_body = invoke_http(
            empty_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
        )
        self.assertEqual(unconfigured_status, 422)
        self.assertEqual(unconfigured_body["rejection_reason_code"], "ais_endpoint_unconfigured")

        mismatch_status, mismatch_body = invoke_http(
            empty_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")

        missing_tenant_status, missing_tenant_body = invoke_http(
            empty_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"idempotency_key": key},
        )
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")

        missing_key_status, missing_key_body = invoke_http(
            empty_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(missing_key_status, 422)
        self.assertEqual(missing_key_body["rejection_reason_code"], "idempotency_key_missing")

        forbidden_app = create_http_app(
            ledger, ais_client=ScriptedAisClient([_lookup_result(403)])
        )
        forbidden_status, forbidden_body = invoke_http(
            forbidden_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
        )
        self.assertEqual(forbidden_status, 422)
        self.assertEqual(forbidden_body["rejection_reason_code"], "cross_tenant")
        self.assertEqual(len(ledger.posting_receipt_observations), 0)

        missing_app = create_http_app(
            ledger, ais_client=ScriptedAisClient([_lookup_result(404)])
        )
        missing_status, missing_body = invoke_http(
            missing_app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "not_yet_accepted")

        get_missing_tenant, get_missing_body = invoke_http(
            empty_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(key, safe='')}",
        )
        self.assertEqual(get_missing_tenant, 422)
        self.assertEqual(get_missing_body["rejection_reason_code"], "tenant_not_found")

        method_status, method_body = invoke_http(empty_app, "GET", "/v1/posting-receipt-observations")
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "tenant_not_found")

        post_item_status, post_item_body = invoke_http(
            empty_app,
            "POST",
            f"/v1/posting-receipt-observations/{quote(key, safe='')}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(post_item_status, 422)
        self.assertEqual(post_item_body["rejection_reason_code"], "request_invalid")

        empty_item_status, empty_item_body = invoke_http(
            empty_app,
            "GET",
            "/v1/posting-receipt-observations/",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(empty_item_status, 404)
        self.assertEqual(empty_item_body["rejection_reason_code"], "route_not_found")

        pin_status, pin_body = invoke_http(
            empty_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(key, safe='')}",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": TENANT_TWO},
        )
        self.assertEqual(pin_status, 422)
        self.assertEqual(pin_body["rejection_reason_code"], "request_invalid")

        with mock.patch(
            "metering_billing.http_app.PostingReceiptPullService.pull_posting_receipt",
            side_effect=ValueError("closed"),
        ):
            value_app = create_http_app(
                ledger, ais_client=ScriptedAisClient([_lookup_result(200, {})])
            )
            value_status, value_body = invoke_http(
                value_app,
                "POST",
                "/v1/posting-receipt-observations",
                {"tenant_reference": TENANT_ONE, "idempotency_key": key},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")

    def test_http_create_app_with_ais_base_url_and_result_helpers(self) -> None:
        """ais_base_url constructs the stdlib client; helpers stay fail-closed."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=key,
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        state = FakeAisState()
        state.receipts[(TENANT_ONE, key)] = receipt
        httpd = start_fake_ais(state)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        app = create_http_app(ledger, ais_base_url=ais_base_url(httpd))
        status, body = invoke_http(
            app,
            "POST",
            "/v1/posting-receipt-observations",
            {"tenant_reference": TENANT_ONE, "idempotency_key": key},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["posting_status_code"], "posted")

        rejected = PostingReceiptObservationResult(
            posting_receipt_observation_outcome_code=PostingReceiptObservationOutcomeCode.REJECTED,
            posting_receipt_observation_id=None,
            receipt_id=None,
            receipt_contract_version=1,
            idempotency_key=None,
            source_proposal_id=None,
            source_payload_hash=None,
            tenant_reference=None,
            legal_entity_reference=None,
            accounting_book_reference=None,
            accounting_policy_version=None,
            posting_rule_version=None,
            posting_status_code=None,
            recorded_at=None,
            fiscal_period_reference=None,
            journal_reference=None,
            reversal_of_journal_reference=None,
            hold_reason_code=None,
            receipt_rejection_reason_code=None,
            posted_at=None,
            line_count=None,
            transaction_currency=None,
            functional_currency=None,
            observed_at=None,
            rejection_reason_code=None,
        )
        rejected_body = rejected.as_contract_dict()
        self.assertEqual(rejected_body["posting_receipt_observation_outcome_code"], "rejected")
        self.assertEqual(rejected_body["rejection_reason_code"], "receipt_invalid")

        with self.assertRaises(ValueError):
            PostingReceiptObservationResult(
                posting_receipt_observation_outcome_code="nope",  # type: ignore[arg-type]
                posting_receipt_observation_id=None,
                receipt_id=None,
                receipt_contract_version=1,
                idempotency_key=None,
                source_proposal_id=None,
                source_payload_hash=None,
                tenant_reference=None,
                legal_entity_reference=None,
                accounting_book_reference=None,
                accounting_policy_version=None,
                posting_rule_version=None,
                posting_status_code=None,
                recorded_at=None,
                fiscal_period_reference=None,
                journal_reference=None,
                reversal_of_journal_reference=None,
                hold_reason_code=None,
                rejection_reason_code=None,
                posted_at=None,
                line_count=None,
                transaction_currency=None,
                functional_currency=None,
                observed_at=None,
                receipt_rejection_reason_code=None,
            ).as_contract_dict()

        with self.assertRaises(PostingReceiptObservationQueryError) as error:
            PostingReceiptPullService(ledger).get_posting_receipt_observation(TENANT_ONE, "")
        self.assertEqual(error.exception.rejection_reason_code, "idempotency_key_missing")
        with self.assertRaises(PostingReceiptObservationQueryError) as tenant_error:
            PostingReceiptPullService(ledger).get_posting_receipt_observation("", key)
        self.assertEqual(tenant_error.exception.rejection_reason_code, "tenant_not_found")
        with self.assertRaises(PostingReceiptObservationQueryError) as missing_catalog:
            PostingReceiptPullService(ledger).get_posting_receipt_observation(
                "urn:cwl:missing_tenant", key
            )
        self.assertEqual(missing_catalog.exception.rejection_reason_code, "tenant_not_found")
        valid_for_insert = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key="insert-conflict-key",
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        with mock.patch.object(
            MemoryUsageLedger,
            "insert_posting_receipt_observation",
            side_effect=ValueError("closed"),
        ):
            insert_conflict = PostingReceiptPullService(
                ledger, ais_client=ScriptedAisClient([_lookup_result(200, valid_for_insert)])
            ).pull_posting_receipt(TENANT_ONE, "insert-conflict-key")
        self.assertEqual(
            insert_conflict.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT,
        )
        with self.assertRaises(ValueError):
            PostingReceiptObservationResult(
                posting_receipt_observation_outcome_code=PostingReceiptObservationOutcomeCode.ACCEPTED,
                posting_receipt_observation_id=None,
                receipt_id=None,
                receipt_contract_version=1,
                idempotency_key=None,
                source_proposal_id=None,
                source_payload_hash=None,
                tenant_reference=None,
                legal_entity_reference=None,
                accounting_book_reference=None,
                accounting_policy_version=None,
                posting_rule_version=None,
                posting_status_code=None,
                recorded_at=None,
                fiscal_period_reference=None,
                journal_reference=None,
                reversal_of_journal_reference=None,
                hold_reason_code=None,
                rejection_reason_code=None,
                posted_at=None,
                line_count=None,
                transaction_currency=None,
                functional_currency=None,
                observed_at=None,
                receipt_rejection_reason_code=None,
            ).as_contract_dict()

        corrupt = StoredPostingReceiptObservation(
            posting_receipt_observation_id=generate_record_id(),
            tenant_account_id=ledger.require_tenant(TENANT_ONE).tenant_account_id,
            receipt_id=uuid4(),
            receipt_contract_version=1,
            idempotency_key="corrupt-key",
            source_proposal_id=uuid4(),
            source_payload_hash="sha256:" + ("b" * 64),
            legal_entity_reference="urn:cwl:entity_001",
            accounting_book_reference="urn:cwl:book_primary",
            accounting_policy_version="policy-2026.1",
            posting_rule_version="rule-2026.1",
            posting_status_code="posted",
            recorded_at="2026-08-17T18:00:00Z",
            fiscal_period_reference=None,
            journal_reference=None,
            reversal_of_journal_reference=None,
            hold_reason_code=None,
            rejection_reason_code=None,
            posted_at=None,
            line_count=None,
            transaction_currency=None,
            functional_currency=None,
            observed_at="not-a-timestamp",
        )
        ledger.insert_posting_receipt_observation(corrupt)
        corrupt_app = create_http_app(ledger)
        corrupt_status, corrupt_body = invoke_http(
            corrupt_app,
            "GET",
            f"/v1/posting-receipt-observations/{quote('corrupt-key', safe='')}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(corrupt_status, 422)
        self.assertEqual(corrupt_body["rejection_reason_code"], "request_invalid")

    def test_ledger_observation_identity_is_append_only(self) -> None:
        """Direct ledger writes replay the same receipt and reject a conflicting one."""
        ledger = MemoryUsageLedger()
        tenant = ledger.register_tenant(TENANT_ONE)
        first = StoredPostingReceiptObservation(
            posting_receipt_observation_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            receipt_id=uuid4(),
            receipt_contract_version=1,
            idempotency_key="key-one",
            source_proposal_id=uuid4(),
            source_payload_hash="sha256:" + ("a" * 64),
            legal_entity_reference="urn:cwl:entity_001",
            accounting_book_reference="urn:cwl:book_primary",
            accounting_policy_version="policy-2026.1",
            posting_rule_version="rule-2026.1",
            posting_status_code="posted",
            recorded_at="2026-08-17T18:00:00Z",
            fiscal_period_reference=None,
            journal_reference=None,
            reversal_of_journal_reference=None,
            hold_reason_code=None,
            rejection_reason_code=None,
            posted_at=None,
            line_count=None,
            transaction_currency=None,
            functional_currency=None,
            observed_at="2026-08-17T18:01:00Z",
        )
        stored = ledger.insert_posting_receipt_observation(first)
        replay = ledger.insert_posting_receipt_observation(replace(first, posting_receipt_observation_id=generate_record_id()))
        with self.assertRaises(ValueError):
            ledger.insert_posting_receipt_observation(replace(first, posting_status_code="posted_maybe"))
        self.assertEqual(stored.posting_receipt_observation_id, first.posting_receipt_observation_id)
        self.assertEqual(replay.posting_receipt_observation_id, first.posting_receipt_observation_id)
        with self.assertRaises(ValueError):
            ledger.insert_posting_receipt_observation(
                replace(first, posting_receipt_observation_id=generate_record_id(), receipt_id=uuid4())
            )
        with self.assertRaises(ValueError):
            ledger.insert_posting_receipt_observation(
                replace(
                    first,
                    posting_receipt_observation_id=generate_record_id(),
                    idempotency_key="key-two",
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_posting_receipt_observation(
                replace(first, idempotency_key="other-id", receipt_id=uuid4())
            )
        found = ledger.find_posting_receipt_observation(tenant.tenant_account_id, "key-one")
        self.assertEqual(found, first)
        self.assertIsNone(ledger.find_posting_receipt_observation(tenant.tenant_account_id, "missing"))
        self.assertEqual(
            ledger.list_posting_receipt_observations(tenant.tenant_account_id),
            (first,),
        )
        self.assertEqual(ledger.list_posting_receipt_observations(), (first,))

        other_key = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key="other-key",
            source_proposal_id=str(first.source_proposal_id),
            source_payload_hash=first.source_payload_hash,
            receipt_id=str(first.receipt_id),
        )
        conflict_service = PostingReceiptPullService(
            ledger, ais_client=ScriptedAisClient([_lookup_result(200, other_key)])
        )
        conflict = conflict_service.pull_posting_receipt(TENANT_ONE, "other-key")
        self.assertEqual(
            conflict.rejection_reason_code,
            PostingReceiptObservationRejectionReasonCode.OBSERVATION_CONFLICT,
        )


if __name__ == "__main__":
    unittest.main()
