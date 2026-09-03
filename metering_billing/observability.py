"""Opt-in OpenTelemetry request tracing with a deliberately small data surface."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import os
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer


OTEL_ENABLED_ENVIRONMENT_VARIABLE = "METERING_BILLING_OTEL_ENABLED"
OTEL_ENDPOINT_ENVIRONMENT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTEL_TRACES_ENDPOINT_ENVIRONMENT_VARIABLE = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
OTEL_SERVICE_NAME_ENVIRONMENT_VARIABLE = "OTEL_SERVICE_NAME"
INSTRUMENTATION_SCOPE = "contextualwisdomlab.metering_billing.http"
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)


def create_tracer(environ: Mapping[str, str] | None = None) -> Tracer:
    """Build the request tracer, exporting only when explicitly enabled."""
    settings = os.environ if environ is None else environ
    enabled = settings.get(OTEL_ENABLED_ENVIRONMENT_VARIABLE, "").strip().lower()
    if enabled not in _ENABLED_VALUES:
        return trace.get_tracer(INSTRUMENTATION_SCOPE)
    traces_endpoint = settings.get(OTEL_TRACES_ENDPOINT_ENVIRONMENT_VARIABLE) or None
    base_endpoint = settings.get(OTEL_ENDPOINT_ENVIRONMENT_VARIABLE)
    endpoint = traces_endpoint or base_endpoint
    if not endpoint:
        raise ValueError(
            f"{OTEL_ENDPOINT_ENVIRONMENT_VARIABLE} must be set when "
            f"{OTEL_ENABLED_ENVIRONMENT_VARIABLE}=true"
        )
    if traces_endpoint is None:
        endpoint = f"{base_endpoint.rstrip('/')}/v1/traces"
    provider = TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: settings.get(
                    OTEL_SERVICE_NAME_ENVIRONMENT_VARIABLE,
                    "metering-billing-platform",
                )
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    return provider.get_tracer(INSTRUMENTATION_SCOPE)


def instrument_wsgi(
    application: Callable[[Mapping[str, Any], Callable[..., Any]], Iterable[bytes]],
    tracer: Tracer,
    route_resolver: Callable[[str, str], tuple[str | None, Any]],
) -> Callable[[Mapping[str, Any], Callable[..., Any]], Iterable[bytes]]:
    """Wrap one WSGI app with safe method, route, status, and duration spans."""

    def wrapped(
        environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        """Create one server span without copying request data into telemetry."""
        raw_method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        method = raw_method if raw_method in _HTTP_METHODS else "OTHER"
        path = str(environ.get("PATH_INFO") or "/")
        route_name, _ = route_resolver(raw_method, path)
        route = route_name or "unknown"
        carrier = {
            key: environ[header]
            for header, key in (
                ("HTTP_TRACEPARENT", "traceparent"),
                ("HTTP_TRACESTATE", "tracestate"),
            )
            if isinstance(environ.get(header), str) and environ[header]
        }
        parent_context: Context = propagate.extract(carrier)
        status_code: int | None = None

        def observed_start_response(
            status: str, headers: list[tuple[str, str]], exc_info: Any = None
        ) -> Any:
            """Capture only the numeric response status for the active span."""
            nonlocal status_code
            status_code = int(status.split(" ", 1)[0])
            if exc_info is None:
                return start_response(status, headers)
            return start_response(status, headers, exc_info)

        with tracer.start_as_current_span(
            f"{method} {route}",
            context=parent_context,
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("http.route", route)
            try:
                result = application(environ, observed_start_response)
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                raise
            if status_code is not None:
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
            return result

    return wrapped
