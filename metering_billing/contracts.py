"""Load and validate the repository's published JSON Schema contracts.

Schemas remain the files under ``schemas/`` so the package and the standalone
repository expose one contract set.  Accounting proposals are re-exported for
importers; this module never invents chart-account identifiers and never
permits a ``posted`` proposal status.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from scripts.validate_repository import (
    validate_accounting_journal_proposal,
    validate_schema_instance,
)

__all__ = (
    "ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME",
    "PROVIDER_CAPABILITY_SCHEMA_NAME",
    "COLLECTION_CASE_SCHEMA_NAME",
    "INVOICE_DRAFT_SCHEMA_NAME",
    "RATING_RUN_SCHEMA_NAME",
    "USAGE_EVENT_SCHEMA_NAME",
    "USAGE_INGESTION_RECEIPT_SCHEMA_NAME",
    "default_schemas_directory",
    "load_json_schema",
    "validate_accounting_journal_proposal",
    "validate_collection_case",
    "validate_invoice_draft",
    "validate_journal_proposal",
    "validate_rating_run",
    "validate_schema_instance",
    "validate_usage_event",
    "validate_usage_ingestion_receipt",
)

USAGE_EVENT_SCHEMA_NAME = "usage-event.schema.json"
USAGE_INGESTION_RECEIPT_SCHEMA_NAME = "usage-ingestion-receipt.schema.json"
RATING_RUN_SCHEMA_NAME = "rating-run.schema.json"
INVOICE_DRAFT_SCHEMA_NAME = "invoice-draft.schema.json"
COLLECTION_CASE_SCHEMA_NAME = "collection-case.schema.json"
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
    """Validate rating-run shape plus identity, reason, and exact total invariants."""
    schema = load_json_schema(RATING_RUN_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, rating_run))
    if not isinstance(rating_run, Mapping):
        return tuple(errors)
    outcome = rating_run.get("rating_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_rating_fields(rating_run, str(outcome)))
        errors.extend(_rating_total_errors(rating_run))
    elif outcome == "rejected":
        if "rejection_reason_code" not in rating_run:
            errors.append("$: rejected rating runs must include rejection_reason_code")
    return tuple(errors)


def _missing_success_rating_fields(
    rating_run: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay run lacks identity."""
    missing: list[str] = []
    for field_name in (
        "rating_run_id",
        "usage_snapshot_hash",
        "rated_total_amount",
        "currency_code",
        "rating_lines",
    ):
        if field_name not in rating_run:
            missing.append(f"$: {outcome} rating runs must include {field_name}")
    return tuple(missing)


def _rating_total_errors(rating_run: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a diagnostic when invoice-intent lines do not sum to the run total."""
    rated_total_amount = rating_run.get("rated_total_amount")
    rating_lines = rating_run.get("rating_lines")
    if not isinstance(rated_total_amount, str) or not isinstance(rating_lines, list):
        return ()
    line_total = Decimal("0")
    for rating_line in rating_lines:
        if not isinstance(rating_line, Mapping):
            return ()
        line_amount = rating_line.get("line_total_amount")
        if not isinstance(line_amount, str):
            return ()
        line_total += Decimal(line_amount)
    if line_total != Decimal(rated_total_amount):
        return ("$: rating line totals must equal rated_total_amount",)
    return ()


def validate_invoice_draft(
    invoice_draft: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate invoice-draft shape plus identity, reason, and exact total invariants."""
    schema = load_json_schema(INVOICE_DRAFT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, invoice_draft))
    if not isinstance(invoice_draft, Mapping):
        return tuple(errors)
    outcome = invoice_draft.get("invoice_draft_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_invoice_draft_fields(invoice_draft, str(outcome)))
        errors.extend(_invoice_draft_total_errors(invoice_draft))
    elif outcome == "rejected":
        if "rejection_reason_code" not in invoice_draft:
            errors.append("$: rejected invoice drafts must include rejection_reason_code")
    return tuple(errors)


def _missing_success_invoice_draft_fields(
    invoice_draft: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay draft lacks identity."""
    missing: list[str] = []
    for field_name in (
        "invoice_draft_id",
        "rating_run_id",
        "drafted_total_amount",
        "currency_code",
        "invoice_draft_status",
        "invoice_draft_lines",
    ):
        if field_name not in invoice_draft:
            missing.append(f"$: {outcome} invoice drafts must include {field_name}")
    return tuple(missing)


def _invoice_draft_total_errors(invoice_draft: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a diagnostic when draft lines do not sum to the drafted total."""
    drafted_total_amount = invoice_draft.get("drafted_total_amount")
    invoice_draft_lines = invoice_draft.get("invoice_draft_lines")
    if not isinstance(drafted_total_amount, str) or not isinstance(invoice_draft_lines, list):
        return ()
    line_total = Decimal("0")
    for invoice_draft_line in invoice_draft_lines:
        if not isinstance(invoice_draft_line, Mapping):
            return ()
        line_amount = invoice_draft_line.get("line_total_amount")
        if not isinstance(line_amount, str):
            return ()
        line_total += Decimal(line_amount)
    if line_total != Decimal(drafted_total_amount):
        return ("$: invoice draft line totals must equal drafted_total_amount",)
    return ()


def validate_collection_case(
    collection_case: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate collection-case shape plus identity, reason, and commercial status."""
    schema = load_json_schema(COLLECTION_CASE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, collection_case))
    if not isinstance(collection_case, Mapping):
        return tuple(errors)
    outcome = collection_case.get("collection_case_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_collection_case_fields(collection_case, str(outcome)))
    elif outcome == "rejected":
        if "rejection_reason_code" not in collection_case:
            errors.append("$: rejected collection cases must include rejection_reason_code")
    return tuple(errors)


def _missing_success_collection_case_fields(
    collection_case: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay case lacks identity."""
    missing: list[str] = []
    for field_name in (
        "collection_case_id",
        "invoice_draft_id",
        "outstanding_amount",
        "currency_code",
        "collection_case_status",
        "dunning_events",
    ):
        if field_name not in collection_case:
            missing.append(f"$: {outcome} collection cases must include {field_name}")
    return tuple(missing)


def validate_journal_proposal(
    proposal: Mapping[str, Any], schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate a proposal-only accounting journal export.

    The helper is a consumer of the existing accounting contract.  It does not
    assign statutory account IDs and it continues to reject ``posted``.
    """
    schema = load_json_schema(ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME, schemas_directory)
    return validate_accounting_journal_proposal(schema, proposal)
