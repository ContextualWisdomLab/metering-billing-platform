"""Sanitized adapters from heterogeneous producer results to usage events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from metering_billing.producer_sdk import build_usage_cloud_event, build_usage_event

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def build_contextual_usage_event(
    record: Mapping[str, Any],
    *,
    tenant_reference: str,
    billing_account_reference: str,
    billing_principal_reference: str,
    credential_reference: str | None = None,
    project_reference: str | None = None,
    cost_center_reference: str | None = None,
) -> dict[str, Any]:
    """Map one contextual-orchestrator usage record without prompt content."""
    usage_record_id = _required_text(record, "usage_record_id")
    workflow_run_id = record.get("workflow_run_id") or "request"
    if not isinstance(workflow_run_id, str) or not workflow_run_id:
        raise ValueError("workflow_run_id must be a non-empty string when present")
    source_event_key = f"contextual-orchestrator:{workflow_run_id}:{usage_record_id}"
    quality = (
        "provider_reported"
        if record.get("measurement_status") == "measured"
        else "estimated"
    )
    provider = _code(record.get("provider_name"), "provider_name")
    route_mode = _code(
        record.get("route_mode") or record.get("request_channel") or "sync",
        "route_mode",
    )
    dimensions = {
        "provider_code": provider,
        "model_code": _required_text(record, "model_name"),
        "workflow_code": "orchestration_workflow",
        "orchestration_mode_code": route_mode,
    }
    event = build_usage_event(
        event_id=_stable_event_id(source_event_key),
        source_event_key=source_event_key,
        tenant_reference=tenant_reference,
        billing_account_reference=billing_account_reference,
        billing_principal_reference=billing_principal_reference,
        product_code="contextual_orchestrator",
        operation_code="complete_step",
        occurred_at=_epoch_timestamp(record.get("created_at")),
        measurements=[
            {
                "meter_code": "gen_ai_input_token",
                "meter_version": 1,
                "quantity": _quantity(record.get("prompt_tokens"), "prompt_tokens"),
                "unit_code": "token",
                "quality_code": quality,
            },
            {
                "meter_code": "gen_ai_output_token",
                "meter_version": 1,
                "quantity": _quantity(
                    record.get("completion_tokens"), "completion_tokens"
                ),
                "unit_code": "token",
                "quality_code": quality,
            },
        ],
        credential_reference=credential_reference,
        project_reference=project_reference,
        cost_center_reference=cost_center_reference,
        dimensions=dimensions,
    )
    return event


def build_contextual_cloud_event(
    record: Mapping[str, Any], **identity: str | None
) -> dict[str, Any]:
    """Build the CloudEvents envelope for one contextual usage record."""
    source = "urn:cwl:producer:contextual-orchestrator"
    return build_usage_cloud_event(
        build_contextual_usage_event(record, **identity), source=source
    )


def build_newsdom_usage_event(
    *,
    tenant_reference: str,
    billing_account_reference: str,
    billing_principal_reference: str,
    document_job_reference: str,
    document_id: str,
    occurred_at: str,
    pdf_bytes: int,
    page_count: int,
    ocr_page_count: int,
    extracted_block_count: int,
    shard_reference: str | None = None,
    credential_reference: str | None = None,
    project_reference: str | None = None,
) -> dict[str, Any]:
    """Map one NewsDOM parse result using counts, never document text."""
    if not document_id:
        raise ValueError("document_id must be non-empty")
    source_event_key = (
        f"newsdom:{document_job_reference}:{shard_reference or document_id}"
    )
    dimensions = {"document_job_reference": document_job_reference}
    if shard_reference is not None:
        dimensions["shard_reference"] = shard_reference
    return build_usage_event(
        event_id=_stable_event_id(source_event_key),
        source_event_key=source_event_key,
        tenant_reference=tenant_reference,
        billing_account_reference=billing_account_reference,
        billing_principal_reference=billing_principal_reference,
        product_code="newsdom_api",
        operation_code="parse_document",
        occurred_at=occurred_at,
        measurements=[
            {
                "meter_code": "document_byte",
                "meter_version": 1,
                "quantity": _quantity(pdf_bytes, "pdf_bytes"),
                "unit_code": "byte",
                "quality_code": "locally_measured",
            },
            {
                "meter_code": "document_page",
                "meter_version": 1,
                "quantity": _quantity(page_count, "page_count"),
                "unit_code": "page",
                "quality_code": "locally_measured",
            },
            {
                "meter_code": "document_ocr_page",
                "meter_version": 1,
                "quantity": _quantity(ocr_page_count, "ocr_page_count"),
                "unit_code": "page",
                "quality_code": "locally_measured",
            },
            {
                "meter_code": "document_extracted_block",
                "meter_version": 1,
                "quantity": _quantity(extracted_block_count, "extracted_block_count"),
                "unit_code": "block",
                "quality_code": "locally_measured",
            },
        ],
        credential_reference=credential_reference,
        project_reference=project_reference,
        dimensions=dimensions,
    )


def build_newsdom_cloud_event(**kwargs: Any) -> dict[str, Any]:
    """Build the CloudEvents envelope for one NewsDOM parse result."""
    return build_usage_cloud_event(
        build_newsdom_usage_event(**kwargs), source="urn:cwl:producer:newsdom-api"
    )


def build_fast_mlsirm_usage_event(
    *,
    tenant_reference: str,
    billing_account_reference: str,
    billing_principal_reference: str,
    run_reference: str,
    artifact_reference: str,
    configuration_reference: str,
    seed_reference: str,
    model_code: str,
    backend_code: str,
    occurred_at: str,
    response_rows: int,
    response_items: int,
    artifact_bytes: int | None = None,
    project_reference: str | None = None,
    credential_reference: str | None = None,
    cost_center_reference: str | None = None,
) -> dict[str, Any]:
    """Map one fast-mlsirm run using dimensions and result sizes only."""
    source_event_key = f"fast-mlsirm:{run_reference}:fit"
    measurements = [
        {
            "meter_code": "analysis_run",
            "meter_version": 1,
            "quantity": "1",
            "unit_code": "run",
            "quality_code": "deterministically_derived",
        },
        {
            "meter_code": "analysis_response_cell",
            "meter_version": 1,
            "quantity": _quantity(
                _product(response_rows, response_items), "response_cells"
            ),
            "unit_code": "cell",
            "quality_code": "locally_measured",
        },
    ]
    if artifact_bytes is not None:
        measurements.append(
            {
                "meter_code": "analysis_artifact_byte",
                "meter_version": 1,
                "quantity": _quantity(artifact_bytes, "artifact_bytes"),
                "unit_code": "byte",
                "quality_code": "locally_measured",
            }
        )
    return build_usage_event(
        event_id=_stable_event_id(source_event_key),
        source_event_key=source_event_key,
        tenant_reference=tenant_reference,
        billing_account_reference=billing_account_reference,
        billing_principal_reference=billing_principal_reference,
        product_code="fast_mlsirm",
        operation_code="fit_model",
        occurred_at=occurred_at,
        measurements=measurements,
        credential_reference=credential_reference,
        cost_center_reference=cost_center_reference,
        project_reference=project_reference,
        dimensions={
            "run_reference": run_reference,
            "artifact_reference": artifact_reference,
            "configuration_reference": configuration_reference,
            "seed_reference": seed_reference,
            "model_code": model_code,
            "backend_code": backend_code,
        },
    )


def build_fast_mlsirm_cloud_event(**kwargs: Any) -> dict[str, Any]:
    """Build the CloudEvents envelope for one fast-mlsirm run."""
    return build_usage_cloud_event(
        build_fast_mlsirm_usage_event(**kwargs), source="urn:cwl:producer:fast-mlsirm"
    )


def _stable_event_id(source_event_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, source_event_key)


def _required_text(record: Mapping[str, Any], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _code(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be lower snake_case")
    return value


def _quantity(value: Any, field_name: str) -> str:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return str(value)


def _product(left: Any, right: Any) -> int:
    if type(left) is not int or type(right) is not int or left < 0 or right < 0:
        raise ValueError("response dimensions must be non-negative integers")
    result = left * right
    if result > 10**18:
        raise ValueError("response cell count is too large")
    return result


def _epoch_timestamp(value: Any) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("created_at must be a non-negative epoch second")
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


__all__ = (
    "build_contextual_cloud_event",
    "build_contextual_usage_event",
    "build_fast_mlsirm_cloud_event",
    "build_fast_mlsirm_usage_event",
    "build_newsdom_cloud_event",
    "build_newsdom_usage_event",
)
