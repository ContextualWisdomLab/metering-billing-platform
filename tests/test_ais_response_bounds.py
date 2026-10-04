"""AIS response admission tests across receipt, outbox, and publish routes."""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError

from metering_billing.posting_receipt import AisPostingReceiptClient, AisTransportError

RESPONSE_LIMIT = 1_048_576
TENANT_REFERENCE = "urn:cwl:tenant_001"


class TrackedResponse(io.BytesIO):
    """Track read bounds and closure on an otherwise ordinary byte stream."""

    status = 200

    def __init__(self, body: bytes) -> None:
        """Seed the byte stream and initialize requested-size and returned-byte tracking."""
        super().__init__(body)
        self.read_sizes: list[int] = []
        self.returned_bytes = 0

    def read(self, size: int = -1) -> bytes:
        """Record the requested and actually returned byte counts."""
        self.read_sizes.append(size)
        result = super().read(size)
        self.returned_bytes += len(result)
        return result


def call_route(client: AisPostingReceiptClient, route: str):
    """Call one of the three production AIS response consumers."""
    if route == "receipt":
        return client.get_posting_receipt(TENANT_REFERENCE, "urn:cwl:receipt_key")
    if route == "outbox":
        return client.list_outbox_events(TENANT_REFERENCE)
    return client.publish_outbox_event(TENANT_REFERENCE, "opaque_event")


@contextmanager
def framed_ais_server(state):
    """Serve exact success or deliberately truncated HTTP framing locally."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Emit fixture bytes without substituting the product HTTP reader."""
            body = state["body"]
            mode = state["mode"]
            self.send_response(200)
            if mode == "length_truncated":
                self.send_header("Content-Length", str(len(body) + 100))
            elif mode == "length_complete":
                self.send_header("Content-Length", str(len(body)))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if mode.startswith("length"):
                self.wfile.write(body)
            else:
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                if mode == "chunked_complete":
                    self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True

        do_POST = do_GET

        def log_message(self, *_args) -> None:
            """Suppress fixture-only server chatter."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("fixture_server_not_settled")


class AisResponseBoundsTests(unittest.TestCase):
    """Reject excess bytes before consumers parse, retain, or act on the body."""

    def test_valid_json_with_short_content_length_is_rejected(self) -> None:
        """Syntactic JSON validity cannot replace HTTP message completeness."""
        state = {"mode": "length_truncated", "body": b'{"outbox_events":[],"next_cursor":null}'}
        with framed_ais_server(state) as origin:
            client = AisPostingReceiptClient(origin, timeout_seconds=2)
            for route in ("receipt", "outbox", "publish"):
                with (
                    self.subTest(route=route),
                    self.assertRaisesRegex(AisTransportError, "^transport_failure$"),
                ):
                    call_route(client, route)

    def test_incomplete_chunked_framing_is_a_transport_denial(self) -> None:
        """A missing final chunk is not a successful JSON response."""
        state = {"mode": "chunked_truncated", "body": b'{"outbox_events":[],"next_cursor":null}'}
        with framed_ais_server(state) as origin:
            client = AisPostingReceiptClient(origin, timeout_seconds=2)
            for route in ("receipt", "outbox", "publish"):
                with (
                    self.subTest(route=route),
                    self.assertRaisesRegex(AisTransportError, "^transport_failure$"),
                ):
                    call_route(client, route)

    def test_framing_rejection_prevents_observation_of_a_valid_receipt(self) -> None:
        """A valid AIS object cannot be persisted from an incomplete message."""
        from metering_billing.posting_receipt import PostingReceiptPullService
        from tests.test_posting_receipt_observation import (
            make_ais_receipt,
            persist_known_ar_and_cash_proposals,
        )

        ledger, proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(proposal.idempotency_key)
        body = json.dumps(make_ais_receipt(
            tenant_reference=TENANT_REFERENCE,
            idempotency_key=key,
            source_proposal_id=str(proposal.proposal_id),
            source_payload_hash=str(proposal.source_payload_hash),
        )).encode()
        state = {"mode": "length_truncated", "body": body}
        with framed_ais_server(state) as origin:
            service = PostingReceiptPullService(
                ledger, ais_client=AisPostingReceiptClient(origin, timeout_seconds=2)
            )
            tenant_id = ledger.require_tenant(TENANT_REFERENCE).tenant_account_id
            for mode in ("length_truncated", "chunked_truncated"):
                with self.subTest(mode=mode):
                    state["mode"] = mode
                    rejected = service.pull_posting_receipt(TENANT_REFERENCE, key)
                    self.assertEqual(rejected.rejection_reason_code.value, "transport_failure")
                    self.assertIsNone(ledger.find_posting_receipt_observation(tenant_id, key))
            state["mode"] = "length_complete"
            accepted = service.pull_posting_receipt(TENANT_REFERENCE, key)
            self.assertEqual(accepted.posting_receipt_observation_outcome_code.value, "accepted")
            state["mode"] = "chunked_complete"
            replay = service.pull_posting_receipt(TENANT_REFERENCE, key)
            self.assertEqual(replay.posting_receipt_observation_outcome_code.value, "duplicate_replay")
            self.assertEqual(replay.posting_receipt_observation_id, accepted.posting_receipt_observation_id)
            self.assertEqual(ledger.get_journal_proposal(proposal.proposal_id).proposal_status, "validated")

    def test_injected_reader_must_return_bytes(self) -> None:
        """An injected transport cannot return text, null, or arbitrary values."""
        for route in ("receipt", "outbox", "publish"):
            for value in (None, "{}", 7):
                with self.subTest(route=route, value=value):
                    class InvalidResponse(TrackedResponse):
                        def read(self, size: int = -1, value=value):
                            """Return only the fixture's invalid transport value."""
                            return value

                    response = InvalidResponse(b"")
                    client = AisPostingReceiptClient(
                        "http://127.0.0.1:9", urlopen=lambda *_args, response=response, **_kwargs: response
                    )
                    with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                        call_route(client, route)
                    self.assertTrue(response.closed)

    def test_complete_framing_at_exact_limit_is_accepted(self) -> None:
        """Complete length/chunked HTTP messages admit the inclusive byte cap."""
        prefix = b'{"outbox_events":[],"next_cursor":null}'
        state = {
            "mode": "length_complete",
            "body": prefix + b" " * (RESPONSE_LIMIT - len(prefix)),
        }
        with framed_ais_server(state) as origin:
            client = AisPostingReceiptClient(origin, timeout_seconds=2)
            for mode in ("length_complete", "chunked_complete"):
                state["mode"] = mode
                for route in ("receipt", "outbox", "publish"):
                    with self.subTest(mode=mode, route=route):
                        result = call_route(client, route)
                        self.assertEqual(result.status_code, 200)
                        if route != "outbox":
                            self.assertEqual(result.raw_body, state["body"])

    def test_exact_limit_valid_bodies_are_accepted_on_all_routes(self) -> None:
        """The documented inclusive ceiling admits otherwise valid JSON bytes."""
        for route in ("receipt", "outbox", "publish"):
            with self.subTest(route=route):
                prefix = b'{"outbox_events":[],"next_cursor":null}' if route == "outbox" else b"{}"
                body = prefix + b" " * (RESPONSE_LIMIT - len(prefix))
                response = TrackedResponse(body)
                result = call_route(AisPostingReceiptClient(
                    "http://127.0.0.1:9", urlopen=lambda *_args, response=response, **_kwargs: response
                ), route)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(response.returned_bytes, RESPONSE_LIMIT)
                self.assertTrue(response.closed)
                if route != "outbox":
                    self.assertEqual(result.raw_body, body)

    def test_real_http_oversize_rejects_and_normal_request_still_works(self) -> None:
        """Default opener enforces the byte ceiling without Content-Length."""
        state = {"oversize": True}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                """Serve valid JSON padded beyond the cap or a normal page."""
                body = json.dumps({"outbox_events": [], "next_cursor": None}).encode()
                if state["oversize"]:
                    body += b" " * (RESPONSE_LIMIT + 100)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_POST = do_GET

            def log_message(self, *_args) -> None:
                """Keep fixture-only request logs out of test output."""

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 2
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            client = AisPostingReceiptClient(
                f"http://127.0.0.1:{server.server_port}", timeout_seconds=2
            )
            for route in ("receipt", "outbox", "publish"):
                with (
                    self.subTest(route=route),
                    self.assertRaisesRegex(AisTransportError, "^transport_failure$"),
                ):
                    call_route(client, route)
            state["oversize"] = False
            self.assertEqual(client.list_outbox_events(TENANT_REFERENCE).outbox_events, ())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_success_read_and_close_failures_are_transport_denials(self) -> None:
        """Cleanup runs after read failure and close failures do not report success."""
        for route in ("receipt", "outbox", "publish"):
            for phase in ("read", "close"):
                with self.subTest(route=route, phase=phase):
                    class BrokenResponse(TrackedResponse):
                        def read(self, size: int = -1, phase=phase) -> bytes:
                            """Inject a read fault only when requested by the fixture."""
                            if phase == "read":
                                raise OSError("fixture_read_failure")
                            return super().read(size)

                        def __exit__(self, *args, phase=phase):
                            """Close first, then inject the fixture cleanup failure."""
                            super().__exit__(*args)
                            if phase == "close":
                                raise OSError("fixture_close_failure")

                    response = BrokenResponse(b'{"outbox_events":[],"next_cursor":null}')
                    client = AisPostingReceiptClient(
                        "http://127.0.0.1:9", urlopen=lambda *_args, response=response, **_kwargs: response
                    )
                    with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                        call_route(client, route)
                    self.assertTrue(response.closed)

    def test_http_error_bodies_are_never_read_and_are_closed(self) -> None:
        """Error mapping must not retain large or malformed remote error bodies."""
        for route in ("receipt", "outbox", "publish"):
            for status in (403, 404, 500):
                with self.subTest(route=route, status=status):
                    body = TrackedResponse(b"invalid" * RESPONSE_LIMIT)
                    error = HTTPError("http://127.0.0.1:9", status, "fixture", None, body)

                    def reject(*_args, error=error, **_kwargs):
                        """Raise the prepared HTTP error so the client must close its body without reading."""
                        raise error

                    client = AisPostingReceiptClient("http://127.0.0.1:9", urlopen=reject)
                    if status == 500:
                        with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                            call_route(client, route)
                    else:
                        self.assertEqual(call_route(client, route).status_code, status)
                    self.assertEqual(body.read_sizes, [])
                    self.assertTrue(body.closed)

    def test_error_close_failure_is_a_stable_transport_denial(self) -> None:
        """A failed error cleanup cannot return a misleading 403/404 receipt."""
        for route in ("receipt", "outbox", "publish"):
            with self.subTest(route=route):
                error = HTTPError("http://127.0.0.1:9", 403, "fixture", None, io.BytesIO(b""))
                close = error.close

                def broken_close(close=close):
                    """Close the real error response first, then raise the fixture cleanup failure."""
                    close()
                    raise OSError("fixture_cleanup_failure")

                error.close = broken_close

                def reject(*_args, error=error, **_kwargs):
                    """Raise the HTTP 403 fixture whose close method fails after closing its body."""
                    raise error

                client = AisPostingReceiptClient("http://127.0.0.1:9", urlopen=reject)
                with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                    call_route(client, route)

    def test_all_routes_stop_at_limit_plus_one_and_close(self) -> None:
        """An oversized response must not cause an unbounded read on any route."""
        for route in ("receipt", "outbox", "publish"):
            with self.subTest(route=route):
                response = TrackedResponse(b"x" * (RESPONSE_LIMIT + 100))
                client = AisPostingReceiptClient(
                    "http://127.0.0.1:9", urlopen=lambda *_args, response=response, **_kwargs: response
                )
                with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                    call_route(client, route)
                self.assertEqual(response.read_sizes, [RESPONSE_LIMIT + 1])
                self.assertEqual(response.returned_bytes, RESPONSE_LIMIT + 1)
                self.assertTrue(response.closed)
