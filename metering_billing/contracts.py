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

from scripts.validate_repository import (
    validate_accounting_journal_proposal,
    validate_schema_instance,
)

__all__ = (
    "ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME",
    "PROVIDER_CAPABILITY_SCHEMA_NAME",
    "USAGE_EVENT_SCHEMA_NAME",
    "USAGE_INGESTION_RECEIPT_SCHEMA_NAME",
    "default_schemas_directory",
    "load_json_schema",
    "validate_accounting_journal_proposal",
    "validate_journal_proposal",
    "validate_schema_instance",
    "validate_usage_event",
    "validate_usage_ingestion_receipt",
)

USAGE_EVENT_SCHEMA_NAME = "usage-event.schema.json"
USAGE_INGESTION_RECEIPT_SCHEMA_NAME = "usage-ingestion-receipt.schema.json"
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
    receipt: Mapping[str, Any], schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate receipt shape plus outcome evidence and count invariants."""
    schema = load_json_schema(USAGE_INGESTION_RECEIPT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, receipt))
    if errors:
        return tuple(errors)

    event_receipts = receipt["event_receipts"]
    accepted = 0
    duplicate_replays = 0
    rejected = 0
    for event_receipt in event_receipts:
        outcome = event_receipt["ingestion_outcome_code"]
        if outcome == "accepted":
            accepted += 1
            if "usage_event_id" not in event_receipt:
                errors.append("$: accepted receipts must include usage_event_id")
        elif outcome == "duplicate_replay":
            duplicate_replays += 1
            if "usage_event_id" not in event_receipt:
                errors.append("$: duplicate_replay receipts must include usage_event_id")
        else:
            rejected += 1
            if "rejection_reason_code" not in event_receipt:
                errors.append("$: rejected receipts must include rejection_reason_code")
    if accepted != receipt["accepted_event_count"]:
        errors.append("$: accepted_event_count must match event_receipts")
    if duplicate_replays != receipt["duplicate_replay_count"]:
        errors.append("$: duplicate_replay_count must match event_receipts")
    if rejected != receipt["rejected_event_count"]:
        errors.append("$: rejected_event_count must match event_receipts")
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
