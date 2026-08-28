# ADR 0126: Opt-in OpenTelemetry Request Tracing

**Status:** Accepted

## Context

Issue #91 requires operational telemetry while forbidding billing content,
tenant identifiers, credentials, provider secrets, and raw exception details
from observability output.  The stdlib WSGI adapter has one central dispatch
boundary, so request tracing can be added there without instrumenting every
commercial service or changing their contracts.

## Decision

- Enable tracing only when `METERING_BILLING_OTEL_ENABLED=true` and require
  `OTEL_EXPORTER_OTLP_ENDPOINT` (or the more specific
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`).  Missing endpoint configuration fails
  closed at application construction; disabled tracing is a no-op.
- Export OTLP/HTTP spans with the service name from `OTEL_SERVICE_NAME`.
  Each server span contains only a bounded HTTP method, the internal route
  name, response status, and the SDK's measured duration.  Request bodies,
  query strings, raw paths, tenant pins, API keys, exception messages, and
  response payloads are never attributes or events.
- Extract only W3C `traceparent` and `tracestate` from the two corresponding
  WSGI headers.  The OpenTelemetry propagator validates them before creating a
  remote parent context; no arbitrary request headers or baggage are copied.
- Mark unhandled exceptions and 5xx responses as span errors without recording
  exception text.  OTel spans are diagnostic signals only and never billing
  source truth.

## Consequences

- A collector can correlate request method/route/status/latency while the
  application remains quiet by default in local Compose runs.
- The first implementation measures request span duration but does not claim
  an SLO or capacity result.  SLO thresholds and collector-backed receipts
  remain release evidence to be measured in the #91 operational rehearsal.
- The API surface, payload schemas, database schema, and provider contracts
  do not change.  Disabling the two environment knobs is the rollback.

## References

- OpenTelemetry Python manual instrumentation and exporter guidance.
- W3C Trace Context propagation through the OpenTelemetry propagator.
