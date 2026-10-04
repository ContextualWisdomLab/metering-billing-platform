"""AIS redirects deny before target I/O on receipt, outbox, and publish routes."""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from metering_billing.posting_receipt import AisPostingReceiptClient, AisTransportError
from tests.test_ais_response_bounds import TENANT_REFERENCE, call_route


@contextmanager
def redirect_ais_servers(state):
    """Serve redirects and count every request reaching either redirect target."""
    target_calls = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Record a second-hop request and return the fixture's valid bytes."""
            target_calls.append((self.command, self.headers.get("X-CWL-Tenant-Reference")))
            self.send_response(200)
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        do_POST = do_GET

        def log_message(self, *_args) -> None:
            """Suppress fixture-only server logs."""

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Send a controlled redirect, or a complete positive response."""
            if state["normal"] or self.path == "/target":
                if self.path == "/target":
                    target_calls.append((self.command, self.headers.get("X-CWL-Tenant-Reference")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(state["body"])))
                self.end_headers()
                self.wfile.write(state["body"])
                return
            self.send_response(state["status"])
            location = (
                "/target" if state["same_origin"]
                else f"http://127.0.0.1:{target.server_port}/target"
            )
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args) -> None:
            """Suppress fixture-only server logs."""

    origin = HTTPServer(("127.0.0.1", 0), Origin)
    servers = (origin, target)
    threads = [threading.Thread(target=server.serve_forever) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{origin.server_port}", target_calls
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("fixture_server_not_settled")


class RedirectAdmissionTests(unittest.TestCase):
    """No redirect can forward tenant context or turn publish into a GET."""

    def test_redirects_reject_before_target_io_on_every_route(self) -> None:
        """Otherwise valid responses do not authorize following a redirect."""
        state = {
            "status": 301, "same_origin": False, "normal": False,
            "body": b'{"outbox_events":[],"next_cursor":null}',
        }
        with redirect_ais_servers(state) as (origin, target_calls):
            client = AisPostingReceiptClient(origin, timeout_seconds=2)
            for same_origin in (False, True):
                state["same_origin"] = same_origin
                for status in (301, 302, 303, 307, 308):
                    state["status"] = status
                    for route in ("receipt", "outbox", "publish"):
                        with self.subTest(same_origin=same_origin, status=status, route=route):
                            target_calls.clear()
                            with self.assertRaisesRegex(AisTransportError, "^transport_failure$"):
                                call_route(client, route)
                            self.assertEqual(target_calls, [], "redirect caused second-hop I/O")
            state["normal"] = True
            for route in ("receipt", "outbox", "publish"):
                self.assertEqual(call_route(client, route).status_code, 200)

    def test_redirect_prevents_valid_receipt_storage_then_normal_replays(self) -> None:
        """Valid target JSON cannot write an observation through a redirect."""
        import json

        from metering_billing.posting_receipt import PostingReceiptPullService
        from tests.test_posting_receipt_observation import (
            make_ais_receipt,
            persist_known_ar_and_cash_proposals,
        )

        ledger, proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(proposal.idempotency_key)
        state = {
            "status": 301, "same_origin": False, "normal": False,
            "body": json.dumps(make_ais_receipt(
                tenant_reference=TENANT_REFERENCE,
                idempotency_key=key,
                source_proposal_id=str(proposal.proposal_id),
                source_payload_hash=str(proposal.source_payload_hash),
            )).encode(),
        }
        with redirect_ais_servers(state) as (origin, target_calls):
            service = PostingReceiptPullService(
                ledger, ais_client=AisPostingReceiptClient(origin, timeout_seconds=2)
            )
            tenant_id = ledger.require_tenant(TENANT_REFERENCE).tenant_account_id
            for same_origin in (False, True):
                state["same_origin"] = same_origin
                for status in (301, 302, 303, 307, 308):
                    state["status"] = status
                    with self.subTest(same_origin=same_origin, status=status):
                        rejected = service.pull_posting_receipt(TENANT_REFERENCE, key)
                        self.assertEqual(rejected.posting_receipt_observation_outcome_code.value, "rejected")
                        self.assertEqual(rejected.rejection_reason_code.value, "transport_failure")
                        self.assertIsNone(ledger.find_posting_receipt_observation(tenant_id, key))
                        self.assertEqual(target_calls, [])
            state["normal"] = True
            accepted = service.pull_posting_receipt(TENANT_REFERENCE, key)
            self.assertEqual(accepted.posting_receipt_observation_outcome_code.value, "accepted")
            replay = service.pull_posting_receipt(TENANT_REFERENCE, key)
            self.assertEqual(replay.posting_receipt_observation_outcome_code.value, "duplicate_replay")
            self.assertEqual(replay.posting_receipt_observation_id, accepted.posting_receipt_observation_id)
            self.assertEqual(ledger.get_journal_proposal(proposal.proposal_id).proposal_status, "validated")


if __name__ == "__main__":
    unittest.main()
