"""Dependency-free producer helpers for the canonical usage contract.

The builder owns only producer-side shaping and integrity.  Ingestion remains
the server-side authority for tenant attribution, deduplication, and durable
receipts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from metering_billing.contracts import validate_usage_event
from metering_billing.payload_integrity import (
    canonical_source_payload_json,
    compute_source_payload_hash,
)

CLOUD_EVENTS_SPECVERSION = "1.0"
USAGE_CLOUD_EVENT_TYPE = "org.contextualwisdomlab.metering.usage.v1"


class ProducerContractError(ValueError):
    """Raised when producer input cannot form a closed usage contract."""


def build_usage_event(
    *,
    event_id: UUID | str,
    source_event_key: str,
    tenant_reference: str,
    billing_account_reference: str,
    billing_principal_reference: str,
    product_code: str,
    occurred_at: str,
    measurements: Sequence[Mapping[str, Any]],
    event_contract_version: int = 1,
    credential_reference: str | None = None,
    cost_center_reference: str | None = None,
    project_reference: str | None = None,
    operation_code: str | None = None,
) -> dict[str, Any]:
    """Build and validate one hash-complete usage event.

    The fixed keyword surface intentionally excludes prompts, responses,
    document text, provider secrets, and arbitrary dimensions.  A producer
    must send those facts through a separately reviewed contract instead of
    smuggling them into billable usage.
    """
    try:
        copied_measurements = []
        for measurement in measurements:
            if not isinstance(measurement, Mapping):
                raise ProducerContractError("measurements must be a sequence of objects")
            copied_measurements.append(dict(measurement))
    except ProducerContractError:
        raise
    except (TypeError, ValueError) as error:
        raise ProducerContractError("measurements must be a sequence of objects") from error

    event: dict[str, Any] = {
        "event_id": str(event_id) if isinstance(event_id, UUID) else event_id,
        "event_contract_version": event_contract_version,
        "source_event_key": source_event_key,
        "tenant_reference": tenant_reference,
        "billing_account_reference": billing_account_reference,
        "billing_principal_reference": billing_principal_reference,
        "product_code": product_code,
        "occurred_at": occurred_at,
        "measurements": copied_measurements,
    }
    for field_name, value in (
        ("credential_reference", credential_reference),
        ("cost_center_reference", cost_center_reference),
        ("project_reference", project_reference),
        ("operation_code", operation_code),
    ):
        if value is not None:
            event[field_name] = value

    try:
        event["source_payload_hash"] = compute_source_payload_hash(event)
    except (KeyError, TypeError, ValueError) as error:
        raise ProducerContractError("usage event cannot be canonically hashed") from error

    errors = validate_usage_event(event)
    if errors:
        raise ProducerContractError("invalid usage event: " + "; ".join(errors))
    return event


def build_usage_cloud_event(event: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Wrap a validated usage event in a CloudEvents 1.0 JSON event."""
    if not isinstance(source, str) or source == "":
        raise ProducerContractError("CloudEvents source must be a non-empty string")
    errors = validate_usage_event(event)
    if errors:
        raise ProducerContractError("invalid usage event: " + "; ".join(errors))
    data = dict(event)
    data["measurements"] = [dict(measurement) for measurement in event["measurements"]]
    return {
        "specversion": CLOUD_EVENTS_SPECVERSION,
        "id": data["event_id"],
        "source": source,
        "type": USAGE_CLOUD_EVENT_TYPE,
        "subject": data["source_event_key"],
        "time": data["occurred_at"],
        "datacontenttype": "application/json",
        "data": data,
    }


__all__ = (
    "CLOUD_EVENTS_SPECVERSION",
    "ProducerContractError",
    "USAGE_CLOUD_EVENT_TYPE",
    "build_usage_cloud_event",
    "build_usage_event",
    "canonical_source_payload_json",
)
