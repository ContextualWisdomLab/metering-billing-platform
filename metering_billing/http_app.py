"""Stdlib HTTP accept surface for the already-built commercial billing path.

The application is a thin WSGI adapter:

1. Parse JSON and require a tenant on every write.
2. Call the existing in-process services.
3. Return each service ``as_contract_dict`` result as JSON.
4. Let AIS pull persisted journal proposals with GET.  Query never mutates
   ``proposal_status``.

Money stays exact-decimal strings.  The adapter never posts a journal, never
stores a card PAN, and never calls a named payment provider.  AIS pulls
validated proposals and later returns ``posting_receipt``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs
from uuid import UUID
from wsgiref.simple_server import make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from metering_billing.accounting_export import AccountingExportService
from metering_billing.collection_case import CollectionCaseService
from metering_billing.errors import ExactDecimalError, JournalProposalQueryError, TimeWindowError
from metering_billing.invoice_draft import InvoiceDraftService
from metering_billing.payment_intent import PaymentIntentService
from metering_billing.payment_settlement import PaymentSettlementService
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ingestion import UsageIngestionService
from metering_billing.usage_ledger import MemoryUsageLedger
from metering_billing.usage_rating import UsageRatingService


WSGIApp = Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]
COLLECTION_DUNNING_PATH = re.compile(
    r"^/v1/collection-cases/([0-9a-fA-F-]{36})/dunning-events$"
)
PAYMENT_CANCEL_PATH = re.compile(r"^/v1/payment-intents/([0-9a-fA-F-]{36})/cancel$")
JOURNAL_PROPOSAL_ITEM_PATH = re.compile(r"^/v1/journal-proposals/([0-9a-fA-F-]{36})$")
KNOWN_POST_PATHS = frozenset(
    {
        "/v1/usage-events",
        "/v1/rating-runs",
        "/v1/invoice-drafts",
        "/v1/journal-proposals",
        "/v1/collection-cases",
        "/v1/payment-intents",
        "/v1/payment-receipts",
        "/v1/cash-journal-proposals",
    }
)
SUCCESS_OUTCOMES = frozenset({"accepted", "duplicate_replay"})


class HttpRequestError(ValueError):
    """Raised when the HTTP adapter cannot decode or authorize a write."""

    def __init__(self, rejection_reason_code: str) -> None:
        super().__init__(rejection_reason_code)
        self.rejection_reason_code = rejection_reason_code


def create_http_app(ledger: MemoryUsageLedger | None = None) -> WSGIApp:
    """Return a stdlib WSGI app bound to one shared commercial ledger."""
    shared_ledger = MemoryUsageLedger() if ledger is None else ledger
    ingestion = UsageIngestionService(shared_ledger)
    rating = UsageRatingService(shared_ledger)
    drafts = InvoiceDraftService(shared_ledger)
    exports = AccountingExportService(shared_ledger)
    collections = CollectionCaseService(shared_ledger)
    intents = PaymentIntentService(shared_ledger)
    settlements = PaymentSettlementService(shared_ledger)

    def application(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        """Dispatch one HTTP request onto the existing commercial services."""
        method = str(environ.get("REQUEST_METHOD") or "GET")
        path = str(environ.get("PATH_INFO") or "/")
        route_name, path_values = _resolve_route(method, path)
        if route_name is None:
            return _send_json(start_response, 404, {"rejection_reason_code": "route_not_found"})
        if route_name == "method_not_allowed":
            return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        if route_name == "healthz":
            return _send_json(start_response, 200, {"status": "ok"})
        if route_name in {"list_journal_proposals", "get_journal_proposal"}:
            try:
                query = _read_query(environ)
                tenant_reference = _require_tenant(query)
                if route_name == "list_journal_proposals":
                    page = exports.list_journal_proposals(
                        tenant_reference,
                        proposal_status=query.get("proposal_status"),
                        proposed_after=query.get("proposed_after"),
                        cursor=query.get("cursor"),
                        page_limit=query.get("page_limit"),
                    )
                    return _send_json(start_response, 200, page.as_contract_dict())
                result = exports.get_journal_proposal(
                    tenant_reference,
                    _parse_uuid(path_values["proposal_id"], "proposal_id"),
                )
                return _send_json(start_response, 200, result.as_contract_dict())
            except JournalProposalQueryError as error:
                status_code = 404 if error.rejection_reason_code == "proposal_not_found" else 422
                return _send_json(
                    start_response,
                    status_code,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except HttpRequestError as error:
                return _send_json(
                    start_response,
                    422,
                    {"rejection_reason_code": error.rejection_reason_code},
                )
            except (ExactDecimalError, TimeWindowError, ValueError):
                return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        try:
            payload = _read_json_object(environ)
            tenant_reference = _require_tenant(payload)
            body, status_code = _dispatch_write(
                route_name,
                path_values,
                tenant_reference,
                payload,
                ingestion,
                rating,
                drafts,
                exports,
                collections,
                intents,
                settlements,
            )
        except HttpRequestError as error:
            return _send_json(
                start_response,
                422,
                {"rejection_reason_code": error.rejection_reason_code},
            )
        except (ExactDecimalError, TimeWindowError, ValueError):
            return _send_json(start_response, 422, {"rejection_reason_code": "request_invalid"})
        return _send_json(start_response, status_code, body)

    return application


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Serve the HTTP accept surface on ``0.0.0.0:$PORT``.

    *arguments* is accepted so tests can invoke the entrypoint without
    touching ``sys.argv``.  The standalone process binds every interface so a
    container or Render web service can reach it.
    """
    del arguments
    port = int(os.environ.get("PORT", "8000"))
    httpd = make_server("0.0.0.0", port, create_http_app())
    httpd.serve_forever()
    return 0


def _resolve_route(method: str, path: str) -> tuple[str | None, dict[str, str]]:
    """Return a route name or mark an unknown / wrong-method path."""
    if path == "/healthz":
        if method == "GET":
            return "healthz", {}
        return "method_not_allowed", {}
    dunning_match = COLLECTION_DUNNING_PATH.fullmatch(path)
    if dunning_match is not None:
        if method == "POST":
            return "dunning_events", {"collection_case_id": dunning_match.group(1)}
        return "method_not_allowed", {}
    cancel_match = PAYMENT_CANCEL_PATH.fullmatch(path)
    if cancel_match is not None:
        if method == "POST":
            return "cancel_payment_intent", {"payment_intent_id": cancel_match.group(1)}
        return "method_not_allowed", {}
    if path == "/v1/journal-proposals":
        if method == "POST":
            return "journal_proposals", {}
        if method == "GET":
            return "list_journal_proposals", {}
        return "method_not_allowed", {}
    proposal_match = JOURNAL_PROPOSAL_ITEM_PATH.fullmatch(path)
    if proposal_match is not None:
        if method == "GET":
            return "get_journal_proposal", {"proposal_id": proposal_match.group(1)}
        return "method_not_allowed", {}
    if path in KNOWN_POST_PATHS:
        if method == "POST":
            return path.removeprefix("/v1/").replace("-", "_"), {}
        return "method_not_allowed", {}
    return None, {}


def _read_query(environ: WSGIEnvironment) -> dict[str, str]:
    """Return the first value for each query-string field."""
    raw_query = str(environ.get("QUERY_STRING") or "")
    parsed = parse_qs(raw_query, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _read_json_object(environ: WSGIEnvironment) -> dict[str, Any]:
    """Read one JSON object from the WSGI input stream."""
    length_text = environ.get("CONTENT_LENGTH") or "0"
    try:
        content_length = int(length_text)
    except ValueError as error:
        raise HttpRequestError("request_invalid") from error
    input_stream = environ.get("wsgi.input")
    if content_length < 0 or input_stream is None:
        raise HttpRequestError("request_invalid")
    raw = input_stream.read(content_length)
    if not raw:
        raise HttpRequestError("request_invalid")
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HttpRequestError("request_invalid") from error
    if not isinstance(loaded, dict):
        raise HttpRequestError("request_invalid")
    return loaded


def _require_tenant(payload: Mapping[str, Any]) -> str:
    """Return the write tenant or reject a request that omitted it."""
    tenant_reference = payload.get("tenant_reference")
    if not isinstance(tenant_reference, str) or not tenant_reference:
        raise HttpRequestError("tenant_not_found")
    return tenant_reference


def _parse_uuid(value: object, field_name: str) -> UUID:
    """Parse a UUID string from a body field or path segment."""
    del field_name
    if not isinstance(value, str):
        raise HttpRequestError("request_invalid")
    try:
        return UUID(value)
    except ValueError as error:
        raise HttpRequestError("request_invalid") from error


def _dispatch_write(
    route_name: str,
    path_values: Mapping[str, str],
    tenant_reference: str,
    payload: Mapping[str, Any],
    ingestion: UsageIngestionService,
    rating: UsageRatingService,
    drafts: InvoiceDraftService,
    exports: AccountingExportService,
    collections: CollectionCaseService,
    intents: PaymentIntentService,
    settlements: PaymentSettlementService,
) -> tuple[dict[str, object], int]:
    """Call one commercial service and map its contract to an HTTP status."""
    if route_name == "usage_events":
        events = payload.get("events")
        if isinstance(events, list):
            receipt = ingestion.ingest_usage_batch(events)
            body = receipt.as_contract_dict()
            accepted = int(body["accepted_event_count"])
            replays = int(body["duplicate_replay_count"])
            rejected = int(body["rejected_event_count"])
            status_code = 422 if rejected > 0 and accepted == 0 and replays == 0 else 200
            return body, status_code
        result = ingestion.ingest_usage_event(payload)
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "rating_runs":
        window = TimeWindow.from_iso8601(
            str(payload.get("window_started_at")),
            str(payload.get("window_ended_at")),
        )
        rate_card_version = payload.get("rate_card_version")
        if not isinstance(rate_card_version, int) or isinstance(rate_card_version, bool):
            raise HttpRequestError("request_invalid")
        result = rating.rate_usage_window(tenant_reference, window, rate_card_version)
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "invoice_drafts":
        result = drafts.draft_invoice(
            tenant_reference, _parse_uuid(payload.get("rating_run_id"), "rating_run_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "journal_proposals":
        result = exports.propose_journal(
            tenant_reference, _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "collection_cases":
        result = collections.open_collection_case(
            tenant_reference, _parse_uuid(payload.get("invoice_draft_id"), "invoice_draft_id")
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "dunning_events":
        notice_code = payload.get("dunning_notice_code")
        if not isinstance(notice_code, str):
            raise HttpRequestError("request_invalid")
        result = collections.record_dunning_event(
            tenant_reference,
            _parse_uuid(path_values["collection_case_id"], "collection_case_id"),
            notice_code,
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "payment_intents":
        result = intents.project_payment_intent(
            tenant_reference,
            _parse_uuid(payload.get("collection_case_id"), "collection_case_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "payment_receipts":
        result = settlements.record_payment_receipt(
            tenant_reference,
            _parse_uuid(payload.get("payment_intent_id"), "payment_intent_id"),
            payload.get("received_amount"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "cancel_payment_intent":
        result = settlements.cancel_payment_intent(
            tenant_reference,
            _parse_uuid(path_values["payment_intent_id"], "payment_intent_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    if route_name == "cash_journal_proposals":
        result = exports.propose_cash_journal(
            tenant_reference,
            _parse_uuid(payload.get("payment_receipt_id"), "payment_receipt_id"),
        )
        return result.as_contract_dict(), _status_for_result(result)
    raise HttpRequestError("request_invalid")


def _status_for_result(result: object) -> int:
    """Map a service outcome to HTTP 200 or 422.

    Published journal proposals omit ``*_outcome_code`` from ``as_contract_dict``.
    Status therefore comes from the in-process result, not from JSON shape.
    """
    for name in dir(result):
        if not name.endswith("_outcome_code"):
            continue
        value = getattr(result, name)
        text = value.value if hasattr(value, "value") else str(value)
        if text in SUCCESS_OUTCOMES:
            return 200
        return 422
    return 422


def _status_for_contract(payload: Mapping[str, object]) -> int:
    """Map accepted and replay outcome fields to 200; everything else stays 422."""
    for key, value in payload.items():
        if not key.endswith("_outcome_code"):
            continue
        if value in SUCCESS_OUTCOMES:
            return 200
        return 422
    if payload.get("proposal_id") and payload.get("proposal_status") != "rejected":
        return 200
    return 422


def _send_json(
    start_response: StartResponse, status_code: int, payload: Mapping[str, object]
) -> Iterable[bytes]:
    """Write a JSON response and return the encoded body."""
    reason = {200: "OK", 404: "Not Found", 422: "Unprocessable Entity"}.get(status_code, "OK")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(
        f"{status_code} {reason}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


if __name__ == "__main__":
    raise SystemExit(main())
