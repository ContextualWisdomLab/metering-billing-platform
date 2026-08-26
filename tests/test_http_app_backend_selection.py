"""Backend-selection tests for the HTTP accept surface (#84 partial progress).

The HTTP adapter must stay bound to the deterministic ``MemoryUsageLedger``
reference adapter unless the environment explicitly selects the durable
``PostgresUsageLedger`` production system of record, and ``GET /readyz`` must
report which backend is serving plus one stable reason code when the
PostgreSQL migration history cannot be probed.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from typing import Any
from urllib.parse import urlencode
from unittest import mock

from metering_billing.http_app import (
    LEDGER_BACKEND_ENVIRONMENT_VARIABLE,
    POSTGRES_DSN_ENVIRONMENT_VARIABLE,
    READYZ_REASON_MIGRATION_HISTORY_UNAVAILABLE,
    create_default_ledger,
    create_http_app,
)
from metering_billing.postgres_usage_ledger import PostgresUsageLedger
from metering_billing.usage_ledger import MemoryUsageLedger


LOCAL_POSTGRES_DSN = os.environ.get(
    "METERING_BILLING_POSTGRES_DSN",
    "postgresql:///metering_billing_test?host=/tmp&port=5433",
)


def invoke_http(
    app: Any,
    method: str,
    path: str,
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call the WSGI app in-process and return status code plus JSON body."""
    raw_body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": urlencode({}),
        "CONTENT_LENGTH": str(len(raw_body)),
        "wsgi.input": io.BytesIO(raw_body),
        "wsgi.errors": io.StringIO(),
        "wsgi.url_scheme": "http",
    }
    if headers is not None:
        for header_name, header_value in headers.items():
            environ["HTTP_" + header_name.upper().replace("-", "_")] = header_value
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


class CreateDefaultLedgerTests(unittest.TestCase):
    """Verify environment-driven ledger selection fails closed and defaults safe."""

    def test_empty_or_memory_selection_returns_reference_adapter(self) -> None:
        """Unset and non-postgres selections both return the memory adapter."""
        self.assertIsInstance(create_default_ledger({}), MemoryUsageLedger)
        self.assertIsInstance(
            create_default_ledger({LEDGER_BACKEND_ENVIRONMENT_VARIABLE: "memory"}),
            MemoryUsageLedger,
        )

    def test_missing_environ_mapping_reads_the_process_environment(self) -> None:
        """``environ=None`` falls back to ``os.environ`` without selecting postgres."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(LEDGER_BACKEND_ENVIRONMENT_VARIABLE, None)
            os.environ.pop(POSTGRES_DSN_ENVIRONMENT_VARIABLE, None)
            self.assertIsInstance(create_default_ledger(), MemoryUsageLedger)

    def test_postgres_selection_without_dsn_fails_closed(self) -> None:
        """A missing or empty DSN raises ValueError naming the variable."""
        with self.assertRaises(ValueError) as missing:
            create_default_ledger(
                {LEDGER_BACKEND_ENVIRONMENT_VARIABLE: "postgres"}
            )
        self.assertIn(POSTGRES_DSN_ENVIRONMENT_VARIABLE, str(missing.exception))

        with self.assertRaises(ValueError) as empty:
            create_default_ledger(
                {
                    LEDGER_BACKEND_ENVIRONMENT_VARIABLE: "postgres",
                    POSTGRES_DSN_ENVIRONMENT_VARIABLE: "",
                }
            )
        self.assertIn(POSTGRES_DSN_ENVIRONMENT_VARIABLE, str(empty.exception))

    def test_postgres_selection_builds_durable_ledger(self) -> None:
        """Both variables set build the PostgreSQL system of record."""
        ledger = create_default_ledger(
            {
                LEDGER_BACKEND_ENVIRONMENT_VARIABLE: "postgres",
                POSTGRES_DSN_ENVIRONMENT_VARIABLE: LOCAL_POSTGRES_DSN,
            }
        )
        try:
            self.assertIsInstance(ledger, PostgresUsageLedger)
            row_count = ledger.migration_history_row_count()
            self.assertIsInstance(row_count, int)
            self.assertGreaterEqual(row_count, 1)
        finally:
            ledger.close()


class ReadyzBackendProbeTests(unittest.TestCase):
    """Verify ``GET /readyz`` reports backend health with stable reason codes."""

    def test_memory_backed_app_is_ready(self) -> None:
        """The deterministic reference adapter answers ready as ``memory``."""
        status, body = invoke_http(create_http_app(), "GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready", "backend": "memory"})

    def test_postgres_backed_app_is_ready_when_migration_history_answers(self) -> None:
        """The durable system of record answers ready as ``postgres``."""
        ledger = PostgresUsageLedger.connect(LOCAL_POSTGRES_DSN)
        try:
            status, body = invoke_http(
                create_http_app(ledger=ledger), "GET", "/readyz"
            )
        finally:
            ledger.close()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready", "backend": "postgres"})

    def test_dead_postgres_session_reports_not_ready_with_reason_code(self) -> None:
        """A failing probe maps onto 503 plus the stable reason code."""
        dead_ledger = PostgresUsageLedger(object())
        app = create_http_app(ledger=dead_ledger)
        status, body = invoke_http(app, "GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["backend"], "postgres")
        self.assertEqual(body["reason"], READYZ_REASON_MIGRATION_HISTORY_UNAVAILABLE)

    def test_postgres_backed_app_serves_authenticated_tenant_reads(self) -> None:
        """The durable backend accepts credential issue plus an authorized read."""
        ledger = PostgresUsageLedger.connect(LOCAL_POSTGRES_DSN)
        try:
            ledger.register_tenant("urn:cwl:k6_http_probe")
            app = create_http_app(ledger=ledger)
            issue_status, issue_body = invoke_http(
                app,
                "POST",
                "/v1/tenant-api-credentials",
                payload={"credential_label": "k6_baseline_runner"},
                headers={"X-CWL-Tenant-Reference": "urn:cwl:k6_http_probe"},
            )
            self.assertEqual(issue_status, 200)
            secret = issue_body["api_credential_secret"]
            read_status, read_body = invoke_http(
                app,
                "GET",
                "/v1/tenant-api-credentials",
                headers={
                    "X-CWL-Tenant-Reference": "urn:cwl:k6_http_probe",
                    "X-CWL-Api-Key": secret,
                },
            )
            self.assertEqual(read_status, 200)
            self.assertEqual(
                read_body["tenant_api_credentials"][0]["credential_label"],
                "k6_baseline_runner",
            )
        finally:
            ledger.close()

    def test_wrong_method_on_readyz_stays_request_invalid(self) -> None:
        """Non-GET readiness requests match the existing health route behavior."""
        status, body = invoke_http(create_http_app(), "POST", "/readyz")
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
