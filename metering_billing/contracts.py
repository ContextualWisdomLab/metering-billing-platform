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
    "PAYMENT_INTENT_SCHEMA_NAME",
    "PAYMENT_RECEIPT_SCHEMA_NAME",
    "CREDIT_ADJUSTMENT_SCHEMA_NAME",
    "RATE_CARD_SCHEMA_NAME",
    "TAX_RATE_SCHEMA_NAME",
    "TAX_ASSESSMENT_SCHEMA_NAME",
    "INVOICE_DRAFT_SCHEMA_NAME",
    "INVOICE_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_CASE_PRESENTMENT_SCHEMA_NAME",
    "TENANT_API_CREDENTIAL_SCHEMA_NAME",
    "WEBHOOK_SUBSCRIPTION_SCHEMA_NAME",
    "WEBHOOK_DELIVERY_SCHEMA_NAME",
    "AIS_OUTBOX_DRAIN_SCHEMA_NAME",
    "RATING_RUN_SCHEMA_NAME",
    "USAGE_EVENT_SCHEMA_NAME",
    "USAGE_INGESTION_RECEIPT_SCHEMA_NAME",
    "default_schemas_directory",
    "load_json_schema",
    "validate_accounting_journal_proposal",
    "validate_collection_case",
    "validate_invoice_draft",
    "validate_invoice_presentment",
    "validate_collection_case_presentment",
    "validate_tenant_api_credential",
    "validate_webhook_subscription",
    "validate_webhook_delivery",
    "validate_ais_outbox_drain",
    "validate_payment_intent",
    "validate_payment_receipt",
    "validate_credit_adjustment",
    "validate_rate_card",
    "validate_tax_rate",
    "validate_tax_assessment",
    "validate_journal_proposal",
    "validate_rating_run",
    "validate_schema_instance",
    "validate_usage_event",
    "validate_usage_ingestion_receipt",
    "ACCOUNTING_POSTING_RECEIPT_SCHEMA_NAME",
    "default_consumed_schemas_directory",
    "validate_consumed_posting_receipt",
)

USAGE_EVENT_SCHEMA_NAME = "usage-event.schema.json"
USAGE_INGESTION_RECEIPT_SCHEMA_NAME = "usage-ingestion-receipt.schema.json"
RATING_RUN_SCHEMA_NAME = "rating-run.schema.json"
INVOICE_DRAFT_SCHEMA_NAME = "invoice-draft.schema.json"
INVOICE_PRESENTMENT_SCHEMA_NAME = "invoice-draft-presentment.schema.json"
COLLECTION_CASE_PRESENTMENT_SCHEMA_NAME = "collection-case-presentment.schema.json"
TENANT_API_CREDENTIAL_SCHEMA_NAME = "tenant-api-credential.schema.json"
WEBHOOK_SUBSCRIPTION_SCHEMA_NAME = "webhook-subscription.schema.json"
WEBHOOK_DELIVERY_SCHEMA_NAME = "webhook-delivery.schema.json"
AIS_OUTBOX_DRAIN_SCHEMA_NAME = "ais-outbox-drain.schema.json"
COLLECTION_CASE_SCHEMA_NAME = "collection-case.schema.json"
PAYMENT_INTENT_SCHEMA_NAME = "payment-intent.schema.json"
PAYMENT_RECEIPT_SCHEMA_NAME = "payment-receipt.schema.json"
CREDIT_ADJUSTMENT_SCHEMA_NAME = "credit-adjustment.schema.json"
RATE_CARD_SCHEMA_NAME = "rate-card.schema.json"
TAX_RATE_SCHEMA_NAME = "tax-rate.schema.json"
TAX_ASSESSMENT_SCHEMA_NAME = "tax-assessment.schema.json"
ACCOUNTING_JOURNAL_PROPOSAL_SCHEMA_NAME = "accounting-journal-proposal.schema.json"
PROVIDER_CAPABILITY_SCHEMA_NAME = "provider-capability.schema.json"
ACCOUNTING_POSTING_RECEIPT_SCHEMA_NAME = "accounting-posting-receipt.schema.json"


def default_schemas_directory() -> Path:
    """Return the repository ``schemas/`` directory next to this package."""
    return Path(__file__).resolve().parents[1] / "schemas"


def default_consumed_schemas_directory() -> Path:
    """Return the consumer-copy directory for contracts Billing does not own."""
    return default_schemas_directory() / "consumed"


def validate_consumed_posting_receipt(
    receipt: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate an AIS posting receipt against the consumed AIS contract.

    Billing does not own this schema.  The helper only checks that a pulled
    response matches the published AIS shape before it is stored.
    """
    directory = (
        default_consumed_schemas_directory()
        if schemas_directory is None
        else schemas_directory
    )
    schema = load_json_schema(ACCOUNTING_POSTING_RECEIPT_SCHEMA_NAME, directory)
    return validate_schema_instance(schema, receipt)


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


def validate_collection_case_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate collection presentment shape plus outstanding and action invariants."""
    schema = load_json_schema(COLLECTION_CASE_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    outstanding = statement.get("collection_outstanding")
    status = statement.get("collection_case_status")
    action = statement.get("next_operator_action")
    if isinstance(outstanding, str):
        try:
            parsed = Decimal(outstanding)
            if parsed < Decimal("0"):
                errors.append("$: collection_outstanding must not be negative")
            if status == "settled" and parsed != Decimal("0"):
                errors.append("$: settled cases must present zero outstanding")
            if status == "settled" and action != "wait":
                errors.append("$: settled cases must wait")
        except Exception:
            errors.append("$: collection_outstanding must be an exact decimal")
    return tuple(errors)


def validate_invoice_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate presentment shape plus tax-sum and amount-due invariants."""
    schema = load_json_schema(INVOICE_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    exclusive = statement.get("tax_exclusive_amount")
    tax_amount = statement.get("tax_amount")
    inclusive = statement.get("tax_inclusive_amount")
    credited = statement.get("credited_amount")
    amount_due = statement.get("amount_due")
    if (
        isinstance(exclusive, str)
        and isinstance(tax_amount, str)
        and isinstance(inclusive, str)
    ):
        try:
            if Decimal(exclusive) + Decimal(tax_amount) != Decimal(inclusive):
                errors.append("$: tax_inclusive_amount must equal exclusive plus tax")
        except Exception:
            errors.append("$: tax amounts must be exact decimals")
    if isinstance(inclusive, str) and isinstance(credited, str) and isinstance(amount_due, str):
        try:
            remaining = Decimal(inclusive) - Decimal(credited)
            expected = remaining if remaining >= Decimal("0") else Decimal("0")
            if Decimal(amount_due) != expected:
                errors.append("$: amount_due must equal inclusive minus credits and not go below zero")
        except Exception:
            errors.append("$: presentment amounts must be exact decimals")
    return tuple(errors)


def validate_tenant_api_credential(
    credential: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate credential shape plus issue-only secret and status invariants."""
    schema = load_json_schema(TENANT_API_CREDENTIAL_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, credential))
    if not isinstance(credential, Mapping):
        return tuple(errors)
    outcome = credential.get("tenant_api_credential_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "tenant_api_credential_id",
            "credential_label",
            "credential_prefix",
            "credential_status",
            "issued_at",
        ):
            if field_name not in credential:
                errors.append(f"$: {outcome} credentials must include {field_name}")
        if "credential_secret_hash" in credential:
            errors.append("$: persisted hashes must not appear on the HTTP contract")
    elif outcome == "rejected":
        if "rejection_reason_code" not in credential:
            errors.append("$: rejected credentials must include rejection_reason_code")
        if "api_credential_secret" in credential:
            errors.append("$: rejected credentials must not include api_credential_secret")
    return tuple(errors)


def validate_webhook_subscription(
    subscription: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate subscription shape plus register-only secret and status invariants."""
    schema = load_json_schema(WEBHOOK_SUBSCRIPTION_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, subscription))
    if not isinstance(subscription, Mapping):
        return tuple(errors)
    outcome = subscription.get("webhook_subscription_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "webhook_subscription_id",
            "callback_url",
            "event_type_codes",
            "webhook_secret_prefix",
            "subscription_status",
            "issued_at",
        ):
            if field_name not in subscription:
                errors.append(f"$: {outcome} subscriptions must include {field_name}")
        if "webhook_secret_hash" in subscription:
            errors.append("$: persisted hashes must not appear on the HTTP contract")
        if outcome == "duplicate_replay" and "webhook_secret" in subscription:
            errors.append("$: replayed subscriptions must not include webhook_secret")
    elif outcome == "rejected":
        if "rejection_reason_code" not in subscription:
            errors.append("$: rejected subscriptions must include rejection_reason_code")
        if "webhook_secret" in subscription:
            errors.append("$: rejected subscriptions must not include webhook_secret")
    return tuple(errors)


def validate_webhook_delivery(
    delivery: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate an explicit delivery-run contract and count invariants."""
    schema = load_json_schema(WEBHOOK_DELIVERY_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, delivery))
    if not isinstance(delivery, Mapping):
        return tuple(errors)
    outcome = delivery.get("webhook_delivery_outcome_code")
    if outcome == "accepted":
        for field_name in (
            "delivered_event_count",
            "attempted_delivery_count",
            "failed_delivery_count",
        ):
            if field_name not in delivery:
                errors.append(f"$: accepted deliveries must include {field_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in delivery:
            errors.append("$: rejected deliveries must include rejection_reason_code")
    return tuple(errors)


def validate_ais_outbox_drain(
    drain: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate an explicit AIS outbox-drain contract and count invariants."""
    schema = load_json_schema(AIS_OUTBOX_DRAIN_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, drain))
    if not isinstance(drain, Mapping):
        return tuple(errors)
    outcome = drain.get("ais_outbox_drain_outcome_code")
    if outcome == "accepted":
        for field_name in (
            "outbox_event_count",
            "receipt_lookup_count",
            "observed_receipt_count",
            "published_event_count",
            "skipped_event_count",
            "next_cursor",
        ):
            if field_name not in drain:
                errors.append(f"$: accepted drains must include {field_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in drain:
            errors.append("$: rejected drains must include rejection_reason_code")
    return tuple(errors)


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


def validate_payment_intent(
    payment_intent: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate payment-intent shape plus identity and projected-only status."""
    schema = load_json_schema(PAYMENT_INTENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, payment_intent))
    if not isinstance(payment_intent, Mapping):
        return tuple(errors)
    outcome = payment_intent.get("payment_intent_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_payment_intent_fields(payment_intent, str(outcome)))
    elif outcome == "rejected":
        if "rejection_reason_code" not in payment_intent:
            errors.append("$: rejected payment intents must include rejection_reason_code")
    return tuple(errors)


def _missing_success_payment_intent_fields(
    payment_intent: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay intent lacks identity."""
    missing: list[str] = []
    for field_name in (
        "payment_intent_id",
        "collection_case_id",
        "payment_amount",
        "currency_code",
        "payment_intent_status",
        "source_payload_hash",
    ):
        if field_name not in payment_intent:
            missing.append(f"$: {outcome} payment intents must include {field_name}")
    return tuple(missing)


def validate_payment_receipt(
    payment_receipt: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate payment-receipt shape plus identity and applied-only status."""
    schema = load_json_schema(PAYMENT_RECEIPT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, payment_receipt))
    if not isinstance(payment_receipt, Mapping):
        return tuple(errors)
    outcome = payment_receipt.get("payment_settlement_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_payment_receipt_fields(payment_receipt, str(outcome)))
    elif outcome == "rejected":
        if "rejection_reason_code" not in payment_receipt:
            errors.append("$: rejected payment receipts must include rejection_reason_code")
    return tuple(errors)


def _missing_success_payment_receipt_fields(
    payment_receipt: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay settlement lacks identity."""
    missing: list[str] = []
    if (
        payment_receipt.get("payment_intent_status") == "cancelled"
        and "payment_receipt_id" not in payment_receipt
    ):
        required_fields = (
            "payment_intent_id",
            "collection_case_id",
            "remaining_outstanding_amount",
            "collection_case_status",
            "next_operator_action",
        )
    else:
        required_fields = (
            "payment_receipt_id",
            "payment_intent_id",
            "collection_case_id",
            "received_amount",
            "remaining_outstanding_amount",
            "currency_code",
            "payment_receipt_status",
            "source_payload_hash",
        )
    for field_name in required_fields:
        if field_name not in payment_receipt:
            missing.append(f"$: {outcome} payment receipts must include {field_name}")
    return tuple(missing)


def validate_credit_adjustment(
    credit_adjustment: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate credit-adjustment shape plus identity and remaining amounts."""
    schema = load_json_schema(CREDIT_ADJUSTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, credit_adjustment))
    if not isinstance(credit_adjustment, Mapping):
        return tuple(errors)
    outcome = credit_adjustment.get("credit_adjustment_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_credit_adjustment_fields(credit_adjustment, str(outcome)))
        credit_amount = credit_adjustment.get("credit_amount")
        exclusive = credit_adjustment.get("tax_exclusive_amount")
        tax_amount = credit_adjustment.get("tax_amount")
        if (
            isinstance(credit_amount, str)
            and isinstance(exclusive, str)
            and isinstance(tax_amount, str)
        ):
            try:
                if Decimal(exclusive) + Decimal(tax_amount) != Decimal(credit_amount):
                    errors.append("$: tax_exclusive_amount plus tax_amount must equal credit_amount")
            except Exception:
                errors.append("$: credit tax amounts must be exact decimals")
    elif outcome == "rejected":
        if "rejection_reason_code" not in credit_adjustment:
            errors.append("$: rejected credit adjustments must include rejection_reason_code")
    return tuple(errors)


def _missing_success_credit_adjustment_fields(
    credit_adjustment: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay credit lacks identity."""
    missing: list[str] = []
    for field_name in (
        "credit_adjustment_id",
        "invoice_draft_id",
        "credit_amount",
        "tax_exclusive_amount",
        "tax_amount",
        "remaining_adjustable_amount",
        "proposal_id",
        "source_payload_hash",
    ):
        if field_name not in credit_adjustment:
            missing.append(f"$: {outcome} credit adjustments must include {field_name}")
    return tuple(missing)


def validate_rate_card(
    rate_card: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate rate-card shape plus identity and published lines."""
    schema = load_json_schema(RATE_CARD_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, rate_card))
    if not isinstance(rate_card, Mapping):
        return tuple(errors)
    outcome = rate_card.get("rate_card_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_rate_card_fields(rate_card, str(outcome)))
    elif outcome == "rejected":
        if "rejection_reason_code" not in rate_card:
            errors.append("$: rejected rate cards must include rejection_reason_code")
    return tuple(errors)


def _missing_success_rate_card_fields(
    rate_card: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay card lacks identity."""
    missing: list[str] = []
    for field_name in (
        "rate_card_id",
        "rate_card_version_id",
        "rate_card_name",
        "rate_card_version",
        "currency_code",
        "source_payload_hash",
        "lines",
    ):
        if field_name not in rate_card:
            missing.append(f"$: {outcome} rate cards must include {field_name}")
    return tuple(missing)


def validate_tax_rate(
    tax_rate: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate tax-rate shape plus identity and published rate."""
    schema = load_json_schema(TAX_RATE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, tax_rate))
    if not isinstance(tax_rate, Mapping):
        return tuple(errors)
    outcome = tax_rate.get("tax_rate_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_tax_rate_fields(tax_rate, str(outcome)))
    elif outcome == "rejected":
        if "rejection_reason_code" not in tax_rate:
            errors.append("$: rejected tax rates must include rejection_reason_code")
    return tuple(errors)


def _missing_success_tax_rate_fields(
    tax_rate: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay rate lacks identity."""
    missing: list[str] = []
    for field_name in (
        "tax_rate_schedule_id",
        "tax_rate_version_id",
        "tax_code",
        "tax_rate_version",
        "tax_rate",
        "source_payload_hash",
    ):
        if field_name not in tax_rate:
            missing.append(f"$: {outcome} tax rates must include {field_name}")
    return tuple(missing)


def validate_tax_assessment(
    tax_assessment: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate tax-assessment shape plus identity and exact amounts."""
    schema = load_json_schema(TAX_ASSESSMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, tax_assessment))
    if not isinstance(tax_assessment, Mapping):
        return tuple(errors)
    outcome = tax_assessment.get("tax_assessment_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_tax_assessment_fields(tax_assessment, str(outcome)))
        exclusive = tax_assessment.get("tax_exclusive_amount")
        tax_amount = tax_assessment.get("tax_amount")
        inclusive = tax_assessment.get("tax_inclusive_amount")
        if (
            isinstance(exclusive, str)
            and isinstance(tax_amount, str)
            and isinstance(inclusive, str)
        ):
            try:
                if Decimal(exclusive) + Decimal(tax_amount) != Decimal(inclusive):
                    errors.append("$: tax_inclusive_amount must equal exclusive plus tax")
            except Exception:
                errors.append("$: tax amounts must be exact decimals")
    elif outcome == "rejected":
        if "rejection_reason_code" not in tax_assessment:
            errors.append("$: rejected tax assessments must include rejection_reason_code")
    return tuple(errors)


def _missing_success_tax_assessment_fields(
    tax_assessment: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay assessment lacks identity."""
    missing: list[str] = []
    for field_name in (
        "tax_assessment_id",
        "invoice_draft_id",
        "tax_rate_version_id",
        "tax_exclusive_amount",
        "tax_amount",
        "tax_inclusive_amount",
        "source_payload_hash",
    ):
        if field_name not in tax_assessment:
            missing.append(f"$: {outcome} tax assessments must include {field_name}")
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
