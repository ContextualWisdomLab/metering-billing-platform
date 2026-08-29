from __future__ import annotations

import unittest
import unittest.mock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from metering_billing.observability import (
    OTEL_ENABLED_ENVIRONMENT_VARIABLE,
    OTEL_ENDPOINT_ENVIRONMENT_VARIABLE,
    OTEL_SERVICE_NAME_ENVIRONMENT_VARIABLE,
    OTEL_TRACES_ENDPOINT_ENVIRONMENT_VARIABLE,
    create_tracer,
    instrument_wsgi,
)


class ObservabilityTests(unittest.TestCase):
    """Verify opt-in tracing and the no-protected-data request boundary."""

    @staticmethod
    def _resolver(method: str, path: str) -> tuple[str | None, object]:
        """Return a safe internal route label for the WSGI test app."""
        del method, path
        return "tenant_read", {}

    def test_disabled_tracing_is_a_noop_and_enabled_requires_endpoint(self) -> None:
        """Keep default apps quiet and fail closed for incomplete opt-in config."""
        tracer = create_tracer({OTEL_ENABLED_ENVIRONMENT_VARIABLE: "false"})
        self.assertIsNotNone(tracer)
        with self.assertRaisesRegex(ValueError, "OTEL_EXPORTER_OTLP_ENDPOINT"):
            create_tracer({OTEL_ENABLED_ENVIRONMENT_VARIABLE: "true"})

    def test_enabled_tracing_builds_otlp_provider(self) -> None:
        """Bind the configured service name and OTLP endpoint."""
        with (
            unittest.mock.patch("metering_billing.observability.OTLPSpanExporter") as exporter,
            unittest.mock.patch.object(TracerProvider, "add_span_processor") as add_processor,
        ):
            tracer = create_tracer(
                {
                    OTEL_ENABLED_ENVIRONMENT_VARIABLE: "on",
                    OTEL_ENDPOINT_ENVIRONMENT_VARIABLE: "http://collector:4318",
                    OTEL_SERVICE_NAME_ENVIRONMENT_VARIABLE: "billing-api",
                }
            )
        self.assertIsNotNone(tracer)
        exporter.assert_called_once_with(endpoint="http://collector:4318/v1/traces")
        add_processor.assert_called_once()

        with unittest.mock.patch(
            "metering_billing.observability.OTLPSpanExporter"
        ) as specific_exporter:
            create_tracer(
                {
                    OTEL_ENABLED_ENVIRONMENT_VARIABLE: "true",
                    OTEL_TRACES_ENDPOINT_ENVIRONMENT_VARIABLE: "http://collector:4318/custom",
                }
            )
        specific_exporter.assert_called_once_with(endpoint="http://collector:4318/custom")

    def test_wsgi_span_extracts_w3c_context_without_request_data(self) -> None:
        """Capture bounded route data and preserve a valid remote trace parent."""
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        def application(environ, start_response):
            """Return a response while asserting the app receives its headers."""
            self.assertEqual(environ["HTTP_X_CWL_TENANT_REFERENCE"], "tenant-secret")
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b'{"ok":true}']

        wrapped = instrument_wsgi(application, provider.get_tracer("test"), self._resolver)
        body = wrapped(
            {
                "REQUEST_METHOD": "get",
                "PATH_INFO": "/v1/tenant-api-credentials/secret-id",
                "HTTP_TRACEPARENT": "00-4bf92f3577b34da6a3ce929d4e0e4736-00f067aa0ba902b7-01",
                "HTTP_TRACESTATE": "vendor=value",
                "HTTP_X_CWL_TENANT_REFERENCE": "tenant-secret",
                "HTTP_X_CWL_API_KEY": "api-secret",
            },
            lambda status, headers, *extra: None,
        )
        self.assertEqual(body, [b'{"ok":true}'])
        span = exporter.get_finished_spans()[0]
        self.assertEqual(span.name, "GET tenant_read")
        self.assertTrue(span.parent.is_remote)
        self.assertEqual(span.attributes["http.request.method"], "GET")
        self.assertEqual(span.attributes["http.route"], "tenant_read")
        self.assertEqual(span.attributes["http.response.status_code"], 200)
        self.assertNotIn("tenant-secret", span.to_json())
        self.assertNotIn("api-secret", span.to_json())

    def test_wsgi_span_marks_failures_without_exception_content(self) -> None:
        """Mark 5xx and raised failures without recording protected messages."""
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        def failing_application(environ, start_response):
            """Return a server error without exposing the request content."""
            del environ
            start_response("503 Service Unavailable", [])
            return [b"not ready"]

        wrapped = instrument_wsgi(
            failing_application, provider.get_tracer("test"), self._resolver
        )
        wrapped({"REQUEST_METHOD": "TRACE", "PATH_INFO": "/readyz"}, lambda *args: None)
        span = exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes["http.request.method"], "TRACE")
        self.assertEqual(span.status.status_code, StatusCode.ERROR)

        def no_response_application(environ, start_response):
            """Return an iterable without starting a response."""
            del environ, start_response
            return [b"no status"]

        instrument_wsgi(
            no_response_application, provider.get_tracer("test"), self._resolver
        )({"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda *args: None)
        no_response_span = exporter.get_finished_spans()[1]
        self.assertNotIn("http.response.status_code", no_response_span.attributes)

        def exc_info_application(environ, start_response):
            """Forward WSGI exception metadata without storing it in the span."""
            del environ
            start_response("404 Not Found", [], (RuntimeError, RuntimeError("x"), None))
            return [b"not found"]

        calls: list[tuple[object, ...]] = []
        instrument_wsgi(
            exc_info_application, provider.get_tracer("test"), self._resolver
        )({"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda *args: calls.append(args))
        self.assertEqual(len(calls[0]), 3)

        def raises_application(environ, start_response):
            """Raise a secret-bearing error that must stay outside telemetry."""
            del environ, start_response
            raise RuntimeError("api-secret-should-not-be-recorded")

        with self.assertRaisesRegex(RuntimeError, "api-secret"):
            instrument_wsgi(
                raises_application, provider.get_tracer("test"), self._resolver
            )({"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda *args: None)
        raised_span = exporter.get_finished_spans()[3]
        self.assertEqual(raised_span.status.status_code, StatusCode.ERROR)
        self.assertEqual(raised_span.events, ())
        self.assertNotIn("api-secret", raised_span.to_json())


if __name__ == "__main__":
    unittest.main()
