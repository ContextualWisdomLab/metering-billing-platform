"""Typed outcomes and rejection reasons for usage ingestion and rating.

Reason codes are stable operational vocabulary.  They are safe to persist in
audit receipts and do not require masking: they describe control failures, not
customer content.
"""

from __future__ import annotations

from enum import StrEnum


class IngestionOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one usage event."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class RejectionReasonCode(StrEnum):
    """Why an event was refused without mutating previously stored usage."""

    SCHEMA_INVALID = "schema_invalid"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    TENANT_NOT_FOUND = "tenant_not_found"
    ATTRIBUTION_TENANT_MISMATCH = "attribution_tenant_mismatch"
    BILLING_ACCOUNT_NOT_FOUND = "billing_account_not_found"
    BILLING_ACCOUNT_NOT_ACTIVE = "billing_account_not_active"
    BILLING_PRINCIPAL_NOT_FOUND = "billing_principal_not_found"
    PRINCIPAL_NOT_EFFECTIVE = "principal_not_effective"
    CREDENTIAL_NOT_FOUND = "credential_not_found"
    CREDENTIAL_NOT_ASSIGNED = "credential_not_assigned"
    METER_NOT_FOUND = "meter_not_found"
    METER_UNIT_MISMATCH = "meter_unit_mismatch"
    METER_QUALITY_NOT_ALLOWED = "meter_quality_not_allowed"
    MEASUREMENT_QUANTITY_INVALID = "measurement_quantity_invalid"
    MEASUREMENT_METER_DUPLICATE = "measurement_meter_duplicate"
    EVENT_OUTSIDE_TIME_WINDOW = "event_outside_time_window"
    SOURCE_EVENT_CONFLICT = "source_event_conflict"
    PAYLOAD_HASH_CONFLICT = "payload_hash_conflict"
    PRODUCER_EVENT_CONFLICT = "producer_event_conflict"


class RatingOutcomeCode(StrEnum):
    """Terminal result of attempting to persist one windowed rating run."""

    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    REJECTED = "rejected"


class RatingRejectionReasonCode(StrEnum):
    """Why a rating request was refused without writing invoice-intent money."""

    TENANT_NOT_FOUND = "tenant_not_found"
    RATE_CARD_NOT_FOUND = "rate_card_not_found"
    RATE_CARD_NOT_EFFECTIVE = "rate_card_not_effective"
    METER_PRICE_MISSING = "meter_price_missing"
    BILLING_DISPOSITION_UNKNOWN = "billing_disposition_unknown"


class ExactDecimalError(ValueError):
    """Raised when a quantity cannot be treated as an exact non-negative decimal."""


class TimeWindowError(ValueError):
    """Raised when a time window or timestamp violates ISO 8601 timezone rules."""
