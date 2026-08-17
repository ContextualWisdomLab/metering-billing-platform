"""Load and validate the repository's published JSON Schema contracts.

Schemas remain the files under ``schemas/`` so the package and the standalone
repository expose one contract set.  Accounting proposals are re-exported for
importers; this module never invents chart-account identifiers and never
permits a ``posted`` proposal status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from metering_billing.errors import ExactDecimalError
from metering_billing.exact_decimal import parse_exact_decimal
from scripts.validate_repository import (
    validate_accounting_journal_proposal,
    validate_schema_instance,
)

__all__ = (
    "ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME",
    "PROVIDER_CAPABILITY_SCHEMA_NAME",
    "RATING_RUN_SCHEMA_NAME",
    "USAGE_EVENT_SCHEMA_NAME",
    "USAGE_INGESTION_RECEIPT_SCHEMA_NAME",
    "default_schemas_directory",
    "load_json_schema",
    "validate_accounting_journal_proposal",
    "validate_journal_proposal",
    "validate_rating_run",
    "validate_schema_instance",
    "validate_usage_event",
    "validate_usage_ingestion_receipt",
)

USAGE_EVENT_SCHEMA_NAME = "usage-event.schema.json"
USAGE_INGESTION_RECEIPT_SCHEMA_NAME = "usage-ingestion-receipt.schema.json"
RATING_RUN_SCHEMA_NAME = "rating-run.schema.json"
ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME = "accounting-journal-proposal.schema.json"
PROVIDER_CAPABILITY_SCHEMA_NAME = "provider-capability.schema.json"


def default_schemas_directory() -> Path:
    """Return the repository ``schemas/`` directory next to this package."""
    return Path(__file__).resolve().parents[1] / "schemas"


def load_json_schema(
    schema_file_name: str, schemas_directory: Path | None = None
) -> dict[str, Any]:
    """Load one Draft 2020-12 schema from the published contract directory."""
    directory = default_schemas_directory() if schemas_directory is None else schemas_directory
    schema_path = directory / schema_file_name
    if not schema_path.is_file():
        raise FileNotFoundError(f"schema contract is not available: {schema_file_name}")
    loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"schema root must be an object: {schema_file_name}")
    return loaded


def validate_usage_event(
    event: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate a usage event against the published usage-event contract."""
    schema = load_json_schema(USAGE_EVENT_SCHEMA_NAME, schemas_directory)
    return validate_schema_instance(schema, event)


def validate_usage_ingestion_receipt(
    receipt: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate receipt shape plus outcome evidence and count invariants."""
    schema = load_json_schema(USAGE_INGESTION_RECEIPT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, receipt))
    if not isinstance(receipt, Mapping):
        return tuple(errors)
    event_receipts = receipt.get("event_receipts")
    if not isinstance(event_receipts, list):
        return tuple(errors)

    accepted = 0
    duplicate_replays = 0
    rejected = 0
    for event_receipt in event_receipts:
        if not isinstance(event_receipt, Mapping):
            continue
        outcome = event_receipt.get("ingestion_outcome_code")
        if outcome == "accepted":
            accepted += 1
            errors.extend(
                _missing_success_receipt_fields(event_receipt, "accepted")
            )
        elif outcome == "duplicate_replay":
            duplicate_replays += 1
            errors.extend(
                _missing_success_receipt_fields(event_receipt, "duplicate_replay")
            )
        elif outcome == "rejected":
            rejected += 1
            if "rejection_reason_code" not in event_receipt:
                errors.append("$: rejected receipts must include rejection_reason_code")
    if accepted != receipt.get("accepted_event_count"):
        errors.append("$: accepted_event_count must match event_receipts")
    if duplicate_replays != receipt.get("duplicate_replay_count"):
        errors.append("$: duplicate_replay_count must match event_receipts")
    if rejected != receipt.get("rejected_event_count"):
        errors.append("$: rejected_event_count must match event_receipts")
    return tuple(errors)


def _missing_success_receipt_fields(
    event_receipt: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay receipt lacks identity."""
    missing: list[str] = []
    for field_name in (
        "usage_event_id",
        "event_contract_version",
        "source_payload_hash",
    ):
        if field_name not in event_receipt:
            missing.append(f"$: {outcome} receipts must include {field_name}")
    return tuple(missing)


def validate_rating_run(
    rating_run: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate rating-run shape plus invoice-intent total invariants."""
    schema = load_json_schema(RATING_RUN_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, rating_run))
    if not isinstance(rating_run, Mapping):
        return tuple(errors)
    rating_lines = rating_run.get("rating_lines")
    if not isinstance(rating_lines, list):
        return tuple(errors)
    try:
        invoice_intent_total = parse_exact_decimal(str(rating_run.get("invoice_intent_total")))
        line_total = sum(
            (
                parse_exact_decimal(str(line["line_amount"]))
                for line in rating_lines
                if isinstance(line, Mapping) and "line_amount" in line
            ),
            parse_exact_decimal("0"),
        )
    except ExactDecimalError:
        return tuple(errors)
    if invoice_intent_total != line_total:
        errors.append("$: invoice_intent_total must equal the sum of rating_lines")
    return tuple(errors)


def validate_journal_proposal(
    proposal: Mapping[str, Any], schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate a proposal-only accounting journal export.

    The helper is a consumer of the existing accounting contract.  It does not
    assign statutory account IDs and it continues to reject ``posted``.
    """
    schema = load_json_schema(ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME, schemas_directory)
    return validate_accounting_journal_proposal(schema, proposal)
