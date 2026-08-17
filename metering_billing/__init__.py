"""Importable commercial contracts, usage-ingestion, and rating services.

This package is the standalone library surface for Contextual Wisdom Lab's
Metering Billing Platform.  Callers can import JSON Schema contracts, ingest
canonical usage events, query tenant-scoped usage windows, and rate those
windows into exact invoice-intent totals without taking a payment-provider
dependency.

The package never posts statutory journals.  Accounting exports remain
``accounting_journal_proposal`` documents with proposal-only statuses.
"""

from metering_billing.contracts import (
    ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME,
    RATING_RUN_SCHEMA_NAME,
    USAGE_EVENT_SCHEMA_NAME,
    USAGE_INGESTION_RECEIPT_SCHEMA_NAME,
    default_schemas_directory,
    load_json_schema,
    validate_rating_run,
    validate_usage_event,
)
from metering_billing.errors import (
    IngestionOutcomeCode,
    RatingOutcomeCode,
    RatingRejectionReasonCode,
    RejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.payload_integrity import compute_source_payload_hash
from metering_billing.time_window import TimeWindow, parse_iso8601_datetime
from metering_billing.usage_ingestion import UsageIngestionService
from metering_billing.usage_ledger import MemoryUsageLedger
from metering_billing.usage_rating import UsageRatingService

__all__ = (
    "ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME",
    "RATING_RUN_SCHEMA_NAME",
    "USAGE_EVENT_SCHEMA_NAME",
    "USAGE_INGESTION_RECEIPT_SCHEMA_NAME",
    "IngestionOutcomeCode",
    "MemoryUsageLedger",
    "RatingOutcomeCode",
    "RatingRejectionReasonCode",
    "RejectionReasonCode",
    "TimeWindow",
    "UsageIngestionService",
    "UsageRatingService",
    "compute_source_payload_hash",
    "default_schemas_directory",
    "format_exact_decimal",
    "load_json_schema",
    "parse_exact_decimal",
    "parse_iso8601_datetime",
    "validate_rating_run",
    "validate_usage_event",
)
