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
    "SPEND_BUDGET_SCHEMA_NAME",
    "SPEND_BUDGET_OVER_SIGNAL_SCHEMA_NAME",
    "SPEND_BUDGET_OVER_SIGNAL_PRESENTMENT_SCHEMA_NAME",
    "SPEND_BUDGET_APPROACHING_SIGNAL_SCHEMA_NAME",
    "SPEND_BUDGET_APPROACHING_SIGNAL_PRESENTMENT_SCHEMA_NAME",
    "SPEND_BUDGET_PRESENTMENT_SCHEMA_NAME",
    "SPEND_BUDGET_EVALUATION_PRESENTMENT_SCHEMA_NAME",
    "BILLING_ACCOUNT_BUDGET_STATUS_PRESENTMENT_SCHEMA_NAME",
    "RATE_CARD_SCHEMA_NAME",
    "TAX_RATE_SCHEMA_NAME",
    "TAX_ASSESSMENT_SCHEMA_NAME",
    "INVOICE_DRAFT_SCHEMA_NAME",
    "INVOICE_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_CASE_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_AGING_PRESENTMENT_SCHEMA_NAME",
    "ACCOUNT_STATEMENT_PRESENTMENT_SCHEMA_NAME",
    "RATED_SPEND_PRESENTMENT_SCHEMA_NAME",
    "PAYMENT_INTENT_PRESENTMENT_SCHEMA_NAME",
    "PAYMENT_RECEIPT_PRESENTMENT_SCHEMA_NAME",
    "CREDIT_ADJUSTMENT_PRESENTMENT_SCHEMA_NAME",
    "RATE_CARD_PRESENTMENT_SCHEMA_NAME",
    "USAGE_EVENT_PRESENTMENT_SCHEMA_NAME",
    "RATING_RUN_PRESENTMENT_SCHEMA_NAME",
    "TENANT_API_CREDENTIAL_SCHEMA_NAME",
    "WEBHOOK_SUBSCRIPTION_SCHEMA_NAME",
    "WEBHOOK_SUBSCRIPTION_PRESENTMENT_SCHEMA_NAME",
    "DUNNING_EVENT_PRESENTMENT_SCHEMA_NAME",
    "WEBHOOK_OUTBOX_EVENT_PRESENTMENT_SCHEMA_NAME",
    "ISSUED_INVOICE_SCHEMA_NAME",
    "ISSUED_INVOICE_PRESENTMENT_SCHEMA_NAME",
    "ISSUED_CREDIT_NOTE_SCHEMA_NAME",
    "ISSUED_CREDIT_NOTE_PRESENTMENT_SCHEMA_NAME",
    "CREDIT_NOTE_APPLICATION_SCHEMA_NAME",
    "CREDIT_NOTE_APPLICATION_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_CASE_SETTLEMENT_SCHEMA_NAME",
    "COLLECTION_CASE_SETTLEMENT_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_WRITE_OFF_SCHEMA_NAME",
    "COLLECTION_WRITE_OFF_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_DISPUTE_SCHEMA_NAME",
    "COLLECTION_DISPUTE_PRESENTMENT_SCHEMA_NAME",
    "COLLECTION_DISPUTE_RELEASE_SCHEMA_NAME",
    "COLLECTION_DISPUTE_RELEASE_PRESENTMENT_SCHEMA_NAME",
    "ISSUED_INVOICE_VOID_SCHEMA_NAME",
    "ISSUED_INVOICE_VOID_PRESENTMENT_SCHEMA_NAME",
    "ISSUED_CREDIT_NOTE_VOID_SCHEMA_NAME",
    "ISSUED_CREDIT_NOTE_VOID_PRESENTMENT_SCHEMA_NAME",
    "UNAPPLIED_CASH_SCHEMA_NAME",
    "UNAPPLIED_CASH_PRESENTMENT_SCHEMA_NAME",
    "UNAPPLIED_CASH_APPLICATION_SCHEMA_NAME",
    "UNAPPLIED_CASH_APPLICATION_PRESENTMENT_SCHEMA_NAME",
    "UNAPPLIED_CASH_REFUND_SCHEMA_NAME",
    "UNAPPLIED_CASH_REFUND_PRESENTMENT_SCHEMA_NAME",
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
    "validate_collection_aging_presentment",
    "validate_account_statement_presentment",
    "validate_rated_spend_presentment",
    "validate_payment_intent_presentment",
    "validate_payment_receipt_presentment",
    "validate_credit_adjustment_presentment",
    "validate_spend_budget_presentment",
    "validate_spend_budget_evaluation_presentment",
    "validate_billing_account_budget_status_presentment",
    "validate_rate_card_presentment",
    "validate_usage_event_presentment",
    "validate_rating_run_presentment",
    "validate_tax_assessment_presentment",
    "validate_posting_receipt_observation_presentment",
    "validate_tenant_api_credential",
    "validate_tenant_api_credential_presentment",
    "validate_webhook_subscription",
    "validate_webhook_delivery",
    "validate_webhook_delivery_presentment",
    "validate_webhook_subscription_presentment",
    "validate_dunning_event_presentment",
    "validate_webhook_outbox_event_presentment",
    "validate_issued_invoice",
    "validate_issued_invoice_presentment",
    "validate_issued_credit_note",
    "validate_issued_credit_note_presentment",
    "validate_credit_note_application",
    "validate_credit_note_application_presentment",
    "validate_collection_case_settlement",
    "validate_collection_case_settlement_presentment",
    "validate_collection_write_off",
    "validate_collection_write_off_presentment",
    "validate_collection_dispute",
    "validate_collection_dispute_presentment",
    "validate_collection_dispute_release",
    "validate_collection_dispute_release_presentment",
    "validate_issued_invoice_void",
    "validate_issued_invoice_void_presentment",
    "validate_issued_credit_note_void",
    "validate_issued_credit_note_void_presentment",
    "validate_unapplied_cash",
    "validate_unapplied_cash_presentment",
    "validate_unapplied_cash_application",
    "validate_unapplied_cash_application_presentment",
    "validate_unapplied_cash_refund",
    "validate_unapplied_cash_refund_presentment",
    "validate_ais_outbox_drain",
    "validate_payment_intent",
    "validate_payment_receipt",
    "validate_credit_adjustment",
    "validate_spend_budget",
    "validate_spend_budget_over_signal",
    "validate_spend_budget_over_signal_presentment",
    "validate_spend_budget_approaching_signal",
    "validate_spend_budget_approaching_signal_presentment",
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
COLLECTION_AGING_PRESENTMENT_SCHEMA_NAME = "collection-aging-presentment.schema.json"
ACCOUNT_STATEMENT_PRESENTMENT_SCHEMA_NAME = "account-statement-presentment.schema.json"
RATED_SPEND_PRESENTMENT_SCHEMA_NAME = "rated-spend-presentment.schema.json"
PAYMENT_INTENT_PRESENTMENT_SCHEMA_NAME = "payment-intent-presentment.schema.json"
PAYMENT_RECEIPT_PRESENTMENT_SCHEMA_NAME = "payment-receipt-presentment.schema.json"
CREDIT_ADJUSTMENT_PRESENTMENT_SCHEMA_NAME = "credit-adjustment-presentment.schema.json"
SPEND_BUDGET_SCHEMA_NAME = "spend-budget.schema.json"
SPEND_BUDGET_OVER_SIGNAL_SCHEMA_NAME = "spend-budget-over-signal.schema.json"
SPEND_BUDGET_OVER_SIGNAL_PRESENTMENT_SCHEMA_NAME = (
    "spend-budget-over-signal-presentment.schema.json"
)
SPEND_BUDGET_APPROACHING_SIGNAL_SCHEMA_NAME = "spend-budget-approaching-signal.schema.json"
SPEND_BUDGET_APPROACHING_SIGNAL_PRESENTMENT_SCHEMA_NAME = (
    "spend-budget-approaching-signal-presentment.schema.json"
)
SPEND_BUDGET_PRESENTMENT_SCHEMA_NAME = "spend-budget-presentment.schema.json"
SPEND_BUDGET_EVALUATION_PRESENTMENT_SCHEMA_NAME = (
    "spend-budget-evaluation-presentment.schema.json"
)
BILLING_ACCOUNT_BUDGET_STATUS_PRESENTMENT_SCHEMA_NAME = (
    "billing-account-budget-status-presentment.schema.json"
)
RATE_CARD_PRESENTMENT_SCHEMA_NAME = "rate-card-presentment.schema.json"
USAGE_EVENT_PRESENTMENT_SCHEMA_NAME = "usage-event-presentment.schema.json"
RATING_RUN_PRESENTMENT_SCHEMA_NAME = "rating-run-presentment.schema.json"
TAX_ASSESSMENT_PRESENTMENT_SCHEMA_NAME = "tax-assessment-presentment.schema.json"
POSTING_RECEIPT_OBSERVATION_PRESENTMENT_SCHEMA_NAME = (
    "posting-receipt-observation-presentment.schema.json"
)
TENANT_API_CREDENTIAL_SCHEMA_NAME = "tenant-api-credential.schema.json"
TENANT_API_CREDENTIAL_PRESENTMENT_SCHEMA_NAME = (
    "tenant-api-credential-presentment.schema.json"
)
WEBHOOK_SUBSCRIPTION_SCHEMA_NAME = "webhook-subscription.schema.json"
WEBHOOK_SUBSCRIPTION_PRESENTMENT_SCHEMA_NAME = (
    "webhook-subscription-presentment.schema.json"
)
DUNNING_EVENT_PRESENTMENT_SCHEMA_NAME = "dunning-event-presentment.schema.json"
WEBHOOK_OUTBOX_EVENT_PRESENTMENT_SCHEMA_NAME = (
    "webhook-outbox-event-presentment.schema.json"
)
ISSUED_INVOICE_SCHEMA_NAME = "issued-invoice.schema.json"
ISSUED_INVOICE_PRESENTMENT_SCHEMA_NAME = "issued-invoice-presentment.schema.json"
ISSUED_CREDIT_NOTE_SCHEMA_NAME = "issued-credit-note.schema.json"
ISSUED_CREDIT_NOTE_PRESENTMENT_SCHEMA_NAME = (
    "issued-credit-note-presentment.schema.json"
)
CREDIT_NOTE_APPLICATION_SCHEMA_NAME = "credit-note-application.schema.json"
CREDIT_NOTE_APPLICATION_PRESENTMENT_SCHEMA_NAME = (
    "credit-note-application-presentment.schema.json"
)
COLLECTION_CASE_SETTLEMENT_SCHEMA_NAME = "collection-case-settlement.schema.json"
COLLECTION_CASE_SETTLEMENT_PRESENTMENT_SCHEMA_NAME = (
    "collection-case-settlement-presentment.schema.json"
)
COLLECTION_WRITE_OFF_SCHEMA_NAME = "collection-write-off.schema.json"
COLLECTION_WRITE_OFF_PRESENTMENT_SCHEMA_NAME = (
    "collection-write-off-presentment.schema.json"
)
COLLECTION_DISPUTE_SCHEMA_NAME = "collection-dispute.schema.json"
COLLECTION_DISPUTE_PRESENTMENT_SCHEMA_NAME = (
    "collection-dispute-presentment.schema.json"
)
COLLECTION_DISPUTE_RELEASE_SCHEMA_NAME = "collection-dispute-release.schema.json"
COLLECTION_DISPUTE_RELEASE_PRESENTMENT_SCHEMA_NAME = (
    "collection-dispute-release-presentment.schema.json"
)
ISSUED_INVOICE_VOID_SCHEMA_NAME = "issued-invoice-void.schema.json"
ISSUED_INVOICE_VOID_PRESENTMENT_SCHEMA_NAME = (
    "issued-invoice-void-presentment.schema.json"
)
ISSUED_CREDIT_NOTE_VOID_SCHEMA_NAME = "issued-credit-note-void.schema.json"
ISSUED_CREDIT_NOTE_VOID_PRESENTMENT_SCHEMA_NAME = (
    "issued-credit-note-void-presentment.schema.json"
)
UNAPPLIED_CASH_SCHEMA_NAME = "unapplied-cash.schema.json"
UNAPPLIED_CASH_PRESENTMENT_SCHEMA_NAME = "unapplied-cash-presentment.schema.json"
UNAPPLIED_CASH_APPLICATION_SCHEMA_NAME = "unapplied-cash-application.schema.json"
UNAPPLIED_CASH_APPLICATION_PRESENTMENT_SCHEMA_NAME = (
    "unapplied-cash-application-presentment.schema.json"
)
UNAPPLIED_CASH_REFUND_SCHEMA_NAME = "unapplied-cash-refund.schema.json"
UNAPPLIED_CASH_REFUND_PRESENTMENT_SCHEMA_NAME = (
    "unapplied-cash-refund-presentment.schema.json"
)
WEBHOOK_DELIVERY_SCHEMA_NAME = "webhook-delivery.schema.json"
WEBHOOK_DELIVERY_PRESENTMENT_SCHEMA_NAME = "webhook-delivery-presentment.schema.json"
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


def validate_account_statement_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate statement shape plus exact totals and one-currency-per-row."""
    schema = load_json_schema(ACCOUNT_STATEMENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    currencies = statement.get("currencies")
    if not isinstance(currencies, list):
        return tuple(errors)
    seen_currencies: set[str] = set()
    amount_fields = (
        "issued_invoice_total",
        "voided_invoice_total",
        "open_collection_remaining",
        "applied_credit_total",
        "voided_credit_total",
        "write_off_total",
        "parked_unapplied_cash",
        "refunded_unapplied_cash",
    )
    for index, row in enumerate(currencies):
        if not isinstance(row, Mapping):
            continue
        currency_code = row.get("currency_code")
        if isinstance(currency_code, str):
            if currency_code in seen_currencies:
                errors.append(f"$.currencies[{index}]: currency_code must be unique")
            seen_currencies.add(currency_code)
        for field_name in amount_fields:
            amount = row.get(field_name)
            if not isinstance(amount, str):
                continue
            try:
                parsed = Decimal(amount)
                if parsed < Decimal("0"):
                    errors.append(
                        f"$.currencies[{index}].{field_name}: amount must not be negative"
                    )
            except Exception:
                errors.append(
                    f"$.currencies[{index}].{field_name}: amount must be an exact decimal"
                )
    return tuple(errors)


def validate_rated_spend_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate spend shape plus exact amounts and one row per grouping key."""
    schema = load_json_schema(RATED_SPEND_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    products = statement.get("products")
    if not isinstance(products, list):
        return tuple(errors)
    seen_keys: set[tuple[str, str, str | None, str | None, str | None, str | None]] = set()
    for index, row in enumerate(products):
        if not isinstance(row, Mapping):
            continue
        currency_code = row.get("currency_code")
        product_code = row.get("product_code")
        project_reference = row.get("project_reference")
        credential_reference = row.get("credential_reference")
        billing_principal_reference = row.get("billing_principal_reference")
        cost_center_reference = row.get("cost_center_reference")
        if isinstance(currency_code, str) and isinstance(product_code, str):
            project_key = project_reference if isinstance(project_reference, str) else None
            credential_key = (
                credential_reference if isinstance(credential_reference, str) else None
            )
            principal_key = (
                billing_principal_reference
                if isinstance(billing_principal_reference, str)
                else None
            )
            cost_center_key = (
                cost_center_reference if isinstance(cost_center_reference, str) else None
            )
            key = (
                currency_code,
                product_code,
                project_key,
                credential_key,
                principal_key,
                cost_center_key,
            )
            if key in seen_keys:
                if cost_center_key is not None:
                    errors.append(
                        "$."
                        f"products[{index}]: currency_code, product_code, and "
                        "cost_center_reference must be unique"
                    )
                elif principal_key is not None:
                    errors.append(
                        "$."
                        f"products[{index}]: currency_code, product_code, and "
                        "billing_principal_reference must be unique"
                    )
                elif credential_key is not None:
                    errors.append(
                        "$."
                        f"products[{index}]: currency_code, product_code, and "
                        "credential_reference must be unique"
                    )
                elif project_key is None:
                    errors.append(
                        f"$.products[{index}]: currency_code and product_code must be unique"
                    )
                else:
                    errors.append(
                        "$."
                        f"products[{index}]: currency_code, product_code, and "
                        "project_reference must be unique"
                    )
            seen_keys.add(key)
        amount = row.get("rated_amount")
        if not isinstance(amount, str):
            continue
        try:
            parsed = Decimal(amount)
            if parsed < Decimal("0"):
                errors.append(
                    f"$.products[{index}].rated_amount: amount must not be negative"
                )
        except Exception:
            errors.append(
                f"$.products[{index}].rated_amount: amount must be an exact decimal"
            )
    return tuple(errors)


def validate_collection_aging_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate aging shape plus exact totals and one-currency-per-row."""
    schema = load_json_schema(COLLECTION_AGING_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    currencies = statement.get("currencies")
    if not isinstance(currencies, list):
        return tuple(errors)
    seen_currencies: set[str] = set()
    for index, row in enumerate(currencies):
        if not isinstance(row, Mapping):
            continue
        currency_code = row.get("currency_code")
        if isinstance(currency_code, str):
            if currency_code in seen_currencies:
                errors.append(f"$.currencies[{index}]: currency_code must be unique")
            seen_currencies.add(currency_code)
        for bucket_name in (
            "current",
            "days_1_30",
            "days_31_60",
            "days_61_90",
            "days_90_plus",
        ):
            bucket = row.get(bucket_name)
            if not isinstance(bucket, Mapping):
                continue
            outstanding = bucket.get("outstanding_amount")
            case_count = bucket.get("case_count")
            if isinstance(outstanding, str):
                try:
                    parsed = Decimal(outstanding)
                    if parsed < Decimal("0"):
                        errors.append(
                            f"$.currencies[{index}].{bucket_name}: outstanding_amount must not be negative"
                        )
                    if case_count == 0 and parsed != Decimal("0"):
                        errors.append(
                            f"$.currencies[{index}].{bucket_name}: empty buckets must be exact zero"
                        )
                except Exception:
                    errors.append(
                        f"$.currencies[{index}].{bucket_name}: outstanding_amount must be an exact decimal"
                    )
    return tuple(errors)


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


def validate_payment_intent_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate payment-intent presentment shape plus amount and action invariants."""
    schema = load_json_schema(PAYMENT_INTENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    amount = statement.get("payment_amount")
    status = statement.get("payment_intent_status")
    action = statement.get("next_operator_action")
    if isinstance(amount, str):
        try:
            parsed = Decimal(amount)
            if parsed < Decimal("0"):
                errors.append("$: payment_amount must not be negative")
            if status == "projected" and action != "record_receipt":
                errors.append("$: projected intents must record a receipt")
            if status in {"cancelled", "rejected"} and action != "wait":
                errors.append("$: cancelled or rejected intents must wait")
        except Exception:
            errors.append("$: payment_amount must be an exact decimal")
    return tuple(errors)


def validate_payment_receipt_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate payment-receipt presentment shape plus amount and action invariants."""
    schema = load_json_schema(PAYMENT_RECEIPT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    received = statement.get("received_amount")
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if isinstance(received, str):
        try:
            parsed_received = Decimal(received)
            if parsed_received < Decimal("0"):
                errors.append("$: received_amount must not be negative")
        except Exception:
            errors.append("$: received_amount must be an exact decimal")
    if isinstance(remaining, str):
        try:
            parsed_remaining = Decimal(remaining)
            if parsed_remaining < Decimal("0"):
                errors.append("$: remaining_outstanding_amount must not be negative")
            if parsed_remaining == Decimal("0") and action != "drain_or_wait":
                errors.append("$: settled receipts must drain or wait")
            if parsed_remaining > Decimal("0") and action != "record_receipt":
                errors.append("$: residual receipts must record another receipt")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    return tuple(errors)


def validate_credit_adjustment_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate credit-adjustment presentment shape plus amount invariants."""
    schema = load_json_schema(CREDIT_ADJUSTMENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    credit_amount = statement.get("credit_amount")
    exclusive = statement.get("tax_exclusive_amount")
    tax_amount = statement.get("tax_amount")
    action = statement.get("next_operator_action")
    status = statement.get("credit_adjustment_status")
    parsed_amounts: list[Decimal] = []
    for field_name, value in (
        ("credit_amount", credit_amount),
        ("tax_exclusive_amount", exclusive),
        ("tax_amount", tax_amount),
    ):
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
                if parsed < Decimal("0"):
                    errors.append(f"$: {field_name} must not be negative")
                else:
                    parsed_amounts.append(parsed)
            except Exception:
                errors.append(f"$: {field_name} must be an exact decimal")
    if len(parsed_amounts) == 3 and parsed_amounts[0] != parsed_amounts[1] + parsed_amounts[2]:
        errors.append("$: tax_exclusive_amount plus tax_amount must equal credit_amount")
    if status == "recorded" and action != "wait":
        errors.append("$: recorded credits must wait")
    return tuple(errors)


def validate_rate_card_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate rate-card presentment shape plus exact unit-price invariants."""
    schema = load_json_schema(RATE_CARD_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    lines = statement.get("lines")
    if isinstance(lines, list):
        for index, line in enumerate(lines):
            if not isinstance(line, Mapping):
                errors.append(f"$: lines[{index}] must be an object")
                continue
            unit_amount = line.get("unit_amount")
            if isinstance(unit_amount, str):
                try:
                    parsed = Decimal(unit_amount)
                    if parsed <= Decimal("0"):
                        errors.append(f"$: lines[{index}].unit_amount must be greater than zero")
                except Exception:
                    errors.append(f"$: lines[{index}].unit_amount must be an exact decimal")
    if action is not None and action != "rate_window":
        errors.append("$: published cards must rate a window")
    return tuple(errors)


def validate_usage_event_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate usage-event presentment shape plus exact quantity invariants."""
    schema = load_json_schema(USAGE_EVENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    measurements = statement.get("measurements")
    if isinstance(measurements, list):
        for index, measurement in enumerate(measurements):
            if not isinstance(measurement, Mapping):
                errors.append(f"$: measurements[{index}] must be an object")
                continue
            quantity = measurement.get("quantity")
            if isinstance(quantity, str):
                try:
                    parsed = Decimal(quantity)
                    if parsed < Decimal("0"):
                        errors.append(f"$: measurements[{index}].quantity must not be negative")
                except Exception:
                    errors.append(f"$: measurements[{index}].quantity must be an exact decimal")
    if action is not None and action != "rate_window":
        errors.append("$: stored usage must rate a window")
    return tuple(errors)


def validate_rating_run_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate rating-run presentment shape plus exact money invariants."""
    schema = load_json_schema(RATING_RUN_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    total = statement.get("rated_total_amount")
    if isinstance(total, str):
        try:
            parsed_total = Decimal(total)
            if parsed_total < Decimal("0"):
                errors.append("$: rated_total_amount must not be negative")
        except Exception:
            errors.append("$: rated_total_amount must be an exact decimal")
    lines = statement.get("rating_lines")
    if isinstance(lines, list):
        for index, line in enumerate(lines):
            if not isinstance(line, Mapping):
                errors.append(f"$: rating_lines[{index}] must be an object")
                continue
            for field_name in ("rated_quantity", "unit_price_amount", "line_total_amount"):
                value = line.get(field_name)
                if isinstance(value, str):
                    try:
                        parsed = Decimal(value)
                        if parsed < Decimal("0"):
                            errors.append(
                                f"$: rating_lines[{index}].{field_name} must not be negative"
                            )
                    except Exception:
                        errors.append(
                            f"$: rating_lines[{index}].{field_name} must be an exact decimal"
                        )
    if action is not None and action != "draft_invoice":
        errors.append("$: stored rating must draft an invoice")
    return tuple(errors)


def validate_tax_assessment_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate tax-assessment presentment shape plus exact money invariants."""
    schema = load_json_schema(TAX_ASSESSMENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    exclusive = statement.get("tax_exclusive_amount")
    tax_amount = statement.get("tax_amount")
    inclusive = statement.get("tax_inclusive_amount")
    tax_rate = statement.get("tax_rate")
    for field_name, value in (
        ("tax_exclusive_amount", exclusive),
        ("tax_amount", tax_amount),
        ("tax_inclusive_amount", inclusive),
        ("tax_rate", tax_rate),
    ):
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
                if parsed < Decimal("0"):
                    errors.append(f"$: {field_name} must not be negative")
            except Exception:
                errors.append(f"$: {field_name} must be an exact decimal")
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
    if action is not None and action != "propose_journal":
        errors.append("$: stored tax must propose a journal")
    return tuple(errors)


def validate_posting_receipt_observation_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate observation presentment shape plus wait-only action."""
    schema = load_json_schema(
        POSTING_RECEIPT_OBSERVATION_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    status_code = statement.get("posting_status_code")
    if action is not None and action != "wait":
        errors.append("$: stored observation must wait")
    if isinstance(status_code, str) and status_code not in {
        "posted",
        "held",
        "rejected",
        "reversed",
    }:
        errors.append("$: posting_status_code must remain an AIS-owned receipt status")
    if "proposal_status" in statement:
        errors.append("$: observation presentment must not claim proposal_status")
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


def validate_tenant_api_credential_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate credential presentment shape plus metadata-only invariants."""
    schema = load_json_schema(
        TENANT_API_CREDENTIAL_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    status_code = statement.get("credential_status")
    if action is not None and action not in {"wait", "issue"}:
        errors.append("$: next_operator_action must be wait or issue")
    if status_code == "active" and action is not None and action != "wait":
        errors.append("$: active credential must wait")
    if status_code == "revoked" and action is not None and action != "issue":
        errors.append("$: revoked credential must issue")
    for forbidden_name in (
        "api_credential_secret",
        "credential_secret_hash",
        "tenant_api_credential_outcome_code",
    ):
        if forbidden_name in statement:
            errors.append(f"$: credential presentment must not include {forbidden_name}")
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


def validate_dunning_event_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate dunning-event presentment shape plus stored-fact invariants."""
    schema = load_json_schema(DUNNING_EVENT_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    if action is not None and action not in {"collect", "wait"}:
        errors.append("$: next_operator_action must be collect or wait")
    for forbidden_name in (
        "recipient",
        "email",
        "phone",
        "channel",
        "provider_id",
        "delivery_status",
        "body",
        "content",
        "sent_at",
        "scheduled_at",
        "notice_amount",
    ):
        if forbidden_name in statement:
            errors.append(f"$: dunning presentment must not include {forbidden_name}")
    return tuple(errors)


def validate_webhook_subscription_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate subscription presentment shape plus metadata-only invariants."""
    schema = load_json_schema(
        WEBHOOK_SUBSCRIPTION_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    status_code = statement.get("subscription_status")
    if action is not None and action not in {"run_deliveries", "register"}:
        errors.append("$: next_operator_action must be run_deliveries or register")
    if status_code == "active" and action is not None and action != "run_deliveries":
        errors.append("$: active subscription must run_deliveries")
    if status_code == "revoked" and action is not None and action != "register":
        errors.append("$: revoked subscription must register")
    for forbidden_name in (
        "webhook_secret",
        "webhook_secret_hash",
        "webhook_secret_prefix",
        "payload_json",
        "webhook_subscription_outcome_code",
    ):
        if forbidden_name in statement:
            errors.append(f"$: subscription presentment must not include {forbidden_name}")
    return tuple(errors)


def validate_webhook_outbox_event_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate outbox-event presentment shape plus metadata-only invariants."""
    schema = load_json_schema(
        WEBHOOK_OUTBOX_EVENT_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    status_code = statement.get("delivery_status")
    if action is not None and action not in {"wait", "run_deliveries"}:
        errors.append("$: next_operator_action must be wait or run_deliveries")
    if status_code == "pending" and action is not None and action != "run_deliveries":
        errors.append("$: pending outbox event must run_deliveries")
    if status_code == "delivered" and action is not None and action != "wait":
        errors.append("$: delivered outbox event must wait")
    for forbidden_name in (
        "payload_json",
        "webhook_secret",
        "webhook_secret_hash",
        "webhook_secret_prefix",
        "signature",
        "card_pan",
        "api_credential_secret",
    ):
        if forbidden_name in statement:
            errors.append(f"$: outbox presentment must not include {forbidden_name}")
    return tuple(errors)


def validate_webhook_delivery_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate delivery presentment shape plus stored-outcome actions."""
    schema = load_json_schema(WEBHOOK_DELIVERY_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    if action is not None and action not in {"wait", "run_deliveries"}:
        errors.append("$: next_operator_action must be wait or run_deliveries")
    if "delivered_at" in statement and action is not None and action != "wait":
        errors.append("$: delivered attempt must wait")
    if "delivered_at" not in statement and action is not None and action != "run_deliveries":
        errors.append("$: failed attempt must run_deliveries")
    for forbidden_name in (
        "webhook_secret",
        "webhook_secret_hash",
        "payload_json",
        "payload_hash",
        "delivery_status",
        "webhook_delivery_status",
    ):
        if forbidden_name in statement:
            errors.append(f"$: delivery presentment must not include {forbidden_name}")
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


def validate_spend_budget_evaluation_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate spend-budget evaluation shape plus remaining/over invariants."""
    schema = load_json_schema(
        SPEND_BUDGET_EVALUATION_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    budget_amount = statement.get("budget_amount")
    rated_amount = statement.get("rated_amount")
    remaining_amount = statement.get("remaining_amount")
    over_amount = statement.get("over_amount")
    utilization_status = statement.get("utilization_status")
    action = statement.get("next_operator_action")
    status = statement.get("spend_budget_status")
    parsed_amounts: dict[str, Decimal] = {}
    for field_name, raw_value in (
        ("budget_amount", budget_amount),
        ("rated_amount", rated_amount),
        ("remaining_amount", remaining_amount),
        ("over_amount", over_amount),
    ):
        if isinstance(raw_value, str):
            try:
                parsed = Decimal(raw_value)
                if parsed < Decimal("0"):
                    errors.append(f"$: {field_name} must be a non-negative exact decimal")
                else:
                    parsed_amounts[field_name] = parsed
            except Exception:
                errors.append(f"$: {field_name} must be an exact decimal")
    if "budget_amount" in parsed_amounts and parsed_amounts["budget_amount"] <= Decimal("0"):
        errors.append("$: budget_amount must be greater than zero")
    if (
        "budget_amount" in parsed_amounts
        and "rated_amount" in parsed_amounts
        and "remaining_amount" in parsed_amounts
        and "over_amount" in parsed_amounts
    ):
        budget = parsed_amounts["budget_amount"]
        rated = parsed_amounts["rated_amount"]
        remaining = parsed_amounts["remaining_amount"]
        over = parsed_amounts["over_amount"]
        if rated < budget:
            expected_remaining = budget - rated
            expected_over = Decimal("0")
            expected_status = "under"
        elif rated == budget:
            expected_remaining = Decimal("0")
            expected_over = Decimal("0")
            expected_status = "at"
        else:
            expected_remaining = Decimal("0")
            expected_over = rated - budget
            expected_status = "over"
        if remaining != expected_remaining or over != expected_over:
            errors.append("$: remaining_amount and over_amount must match budget minus rated")
        if utilization_status != expected_status:
            errors.append("$: utilization_status must match remaining and over")
    if status == "published" and action != "wait":
        errors.append("$: published spend budget evaluations must wait")
    if "card_pan" in statement:
        errors.append("$: spend budget evaluation must not include card_pan")
    if "retained_earnings" in statement:
        errors.append("$: spend budget evaluation must not include retained_earnings")
    return tuple(errors)


def validate_billing_account_budget_status_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate the account-level budget-status page plus remaining/over math."""
    schema = load_json_schema(
        BILLING_ACCOUNT_BUDGET_STATUS_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    if "rated_amount" in statement:
        errors.append("$: budget status page must not mix currencies into one rated_amount")
    if "card_pan" in statement:
        errors.append("$: spend budget status must not include card_pan")
    if "retained_earnings" in statement:
        errors.append("$: spend budget status must not include retained_earnings")
    rows = statement.get("budget_statuses")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            errors.extend(
                _budget_status_row_errors(row, prefix=f"$.budget_statuses[{index}]")
            )
    return tuple(errors)


def _budget_status_row_errors(row: Mapping[str, Any], prefix: str) -> tuple[str, ...]:
    """Return remaining/over invariant errors for one budget-status row."""
    errors: list[str] = []
    budget_amount = row.get("budget_amount")
    rated_amount = row.get("rated_amount")
    remaining_amount = row.get("remaining_amount")
    over_amount = row.get("over_amount")
    utilization_status = row.get("utilization_status")
    action = row.get("next_operator_action")
    status = row.get("spend_budget_status")
    parsed_amounts: dict[str, Decimal] = {}
    for field_name, raw_value in (
        ("budget_amount", budget_amount),
        ("rated_amount", rated_amount),
        ("remaining_amount", remaining_amount),
        ("over_amount", over_amount),
    ):
        if isinstance(raw_value, str):
            try:
                parsed = Decimal(raw_value)
                if parsed < Decimal("0"):
                    errors.append(f"{prefix}: {field_name} must be a non-negative exact decimal")
                else:
                    parsed_amounts[field_name] = parsed
            except Exception:
                errors.append(f"{prefix}: {field_name} must be an exact decimal")
    if "budget_amount" in parsed_amounts and parsed_amounts["budget_amount"] <= Decimal("0"):
        errors.append(f"{prefix}: budget_amount must be greater than zero")
    if (
        "budget_amount" in parsed_amounts
        and "rated_amount" in parsed_amounts
        and "remaining_amount" in parsed_amounts
        and "over_amount" in parsed_amounts
    ):
        budget = parsed_amounts["budget_amount"]
        rated = parsed_amounts["rated_amount"]
        remaining = parsed_amounts["remaining_amount"]
        over = parsed_amounts["over_amount"]
        if rated < budget:
            expected_remaining = budget - rated
            expected_over = Decimal("0")
            expected_status = "under"
        elif rated == budget:
            expected_remaining = Decimal("0")
            expected_over = Decimal("0")
            expected_status = "at"
        else:
            expected_remaining = Decimal("0")
            expected_over = rated - budget
            expected_status = "over"
        if remaining != expected_remaining or over != expected_over:
            errors.append(
                f"{prefix}: remaining_amount and over_amount must match budget minus rated"
            )
        if utilization_status != expected_status:
            errors.append(f"{prefix}: utilization_status must match remaining and over")
    if status == "published" and action != "wait":
        errors.append(f"{prefix}: published spend budget statuses must wait")
    if "card_pan" in row:
        errors.append(f"{prefix}: spend budget status must not include card_pan")
    if "retained_earnings" in row:
        errors.append(f"{prefix}: spend budget status must not include retained_earnings")
    return tuple(errors)


def validate_spend_budget_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate spend-budget presentment shape plus amount invariants."""
    schema = load_json_schema(SPEND_BUDGET_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    budget_amount = statement.get("budget_amount")
    action = statement.get("next_operator_action")
    status = statement.get("spend_budget_status")
    if isinstance(budget_amount, str):
        try:
            parsed = Decimal(budget_amount)
            if parsed <= Decimal("0"):
                errors.append("$: budget_amount must be greater than zero")
        except Exception:
            errors.append("$: budget_amount must be an exact decimal")
    if status == "published" and action != "wait":
        errors.append("$: published spend budgets must wait")
    if "card_pan" in statement:
        errors.append("$: spend budget presentment must not include card_pan")
    return tuple(errors)


def validate_spend_budget(
    spend_budget: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate spend-budget shape plus identity and published amount."""
    schema = load_json_schema(SPEND_BUDGET_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, spend_budget))
    if not isinstance(spend_budget, Mapping):
        return tuple(errors)
    outcome = spend_budget.get("spend_budget_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_spend_budget_fields(spend_budget, str(outcome)))
        budget_amount = spend_budget.get("budget_amount")
        if isinstance(budget_amount, str):
            try:
                if Decimal(budget_amount) <= Decimal("0"):
                    errors.append("$: budget_amount must be greater than zero")
            except Exception:
                errors.append("$: budget_amount must be an exact decimal")
        if spend_budget.get("spend_budget_status") == "published":
            if spend_budget.get("next_operator_action") != "wait":
                errors.append("$: published spend budgets must wait")
    elif outcome == "rejected":
        if "rejection_reason_code" not in spend_budget:
            errors.append("$: rejected spend budgets must include rejection_reason_code")
    if "card_pan" in spend_budget:
        errors.append("$: spend budget must not include card_pan")
    if "retained_earnings" in spend_budget:
        errors.append("$: spend budget must not include retained_earnings")
    return tuple(errors)


def validate_spend_budget_over_signal(
    over_signal: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate over-signal shape plus identity, exact over, and utilization."""
    schema = load_json_schema(SPEND_BUDGET_OVER_SIGNAL_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, over_signal))
    if not isinstance(over_signal, Mapping):
        return tuple(errors)
    outcome = over_signal.get("spend_budget_over_signal_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_spend_budget_over_signal_fields(over_signal, str(outcome)))
        budget_amount = over_signal.get("budget_amount")
        over_amount = over_signal.get("over_amount")
        utilization_status = over_signal.get("utilization_status")
        parsed_amounts: dict[str, Decimal] = {}
        for field_name, raw_value in (
            ("budget_amount", budget_amount),
            ("over_amount", over_amount),
        ):
            if isinstance(raw_value, str):
                try:
                    parsed = Decimal(raw_value)
                    if parsed < Decimal("0"):
                        errors.append(f"$: {field_name} must be a non-negative exact decimal")
                    else:
                        parsed_amounts[field_name] = parsed
                except Exception:
                    errors.append(f"$: {field_name} must be an exact decimal")
        if "budget_amount" in parsed_amounts and parsed_amounts["budget_amount"] <= Decimal("0"):
            errors.append("$: budget_amount must be greater than zero")
        if "over_amount" in parsed_amounts and utilization_status == "over":
            if parsed_amounts["over_amount"] <= Decimal("0"):
                errors.append("$: over observations must include a positive over_amount")
        if "over_amount" in parsed_amounts and utilization_status in {"under", "at"}:
            if parsed_amounts["over_amount"] != Decimal("0"):
                errors.append("$: under and at observations must have zero over_amount")
        if over_signal.get("spend_budget_status") == "published":
            if over_signal.get("next_operator_action") != "wait":
                errors.append("$: published spend budgets must wait")
    elif outcome == "rejected":
        if "rejection_reason_code" not in over_signal:
            errors.append("$: rejected over signals must include rejection_reason_code")
    if "card_pan" in over_signal:
        errors.append("$: spend budget over signal must not include card_pan")
    if "retained_earnings" in over_signal:
        errors.append("$: spend budget over signal must not include retained_earnings")
    if "remaining_amount" in over_signal:
        errors.append("$: spend budget over signal must not include remaining_amount")
    return tuple(errors)


def _missing_success_spend_budget_over_signal_fields(
    over_signal: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay over-signal lacks identity."""
    missing: list[str] = []
    for field_name in (
        "spend_budget_id",
        "tenant_reference",
        "billing_account_id",
        "currency_code",
        "budget_amount",
        "over_amount",
        "utilization_status",
        "window_started_at",
        "window_ended_at",
        "spend_budget_status",
        "source_payload_hash",
        "spend_budget_contract_version",
    ):
        if field_name not in over_signal:
            missing.append(f"$: {outcome} over signals must include {field_name}")
    return tuple(missing)


def validate_spend_budget_over_signal_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate live over-signal presentment plus nested existing envelopes."""
    schema = load_json_schema(
        SPEND_BUDGET_OVER_SIGNAL_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    if "remaining_amount" in statement:
        errors.append("$: over-signal presentment must not include remaining_amount")
    if "card_pan" in statement:
        errors.append("$: over-signal presentment must not include card_pan")
    if "retained_earnings" in statement:
        errors.append("$: over-signal presentment must not include retained_earnings")
    if "payload_json" in statement:
        errors.append("$: over-signal presentment must not include payload_json")
    over_signal = statement.get("over_signal")
    spend_budget_id = None
    if isinstance(over_signal, Mapping):
        errors.extend(validate_spend_budget_over_signal(over_signal, schemas_directory))
        raw_budget_id = over_signal.get("spend_budget_id")
        if isinstance(raw_budget_id, str):
            spend_budget_id = raw_budget_id
    rows = statement.get("webhook_outbox_events")
    if isinstance(rows, list):
        if len(rows) > 1:
            errors.append("$: over-signal presentment has at most one spend_budget.over row")
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            errors.extend(validate_webhook_outbox_event_presentment(row, schemas_directory))
            event_type = row.get("event_type_code")
            if event_type is not None and event_type != "spend_budget.over":
                errors.append(
                    f"$.webhook_outbox_events[{index}]: event_type_code must be spend_budget.over"
                )
            source_id = row.get("source_id")
            if (
                spend_budget_id is not None
                and source_id is not None
                and source_id != spend_budget_id
            ):
                errors.append(
                    f"$.webhook_outbox_events[{index}]: source_id must match over_signal.spend_budget_id"
                )
    return tuple(errors)


def validate_spend_budget_approaching_signal(
    approaching_signal: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate approaching-signal shape plus identity, exact remaining, and utilization."""
    schema = load_json_schema(SPEND_BUDGET_APPROACHING_SIGNAL_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, approaching_signal))
    if not isinstance(approaching_signal, Mapping):
        return tuple(errors)
    outcome = approaching_signal.get("spend_budget_approaching_signal_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(
            _missing_success_spend_budget_approaching_signal_fields(
                approaching_signal, str(outcome)
            )
        )
        budget_amount = approaching_signal.get("budget_amount")
        remaining_amount = approaching_signal.get("remaining_amount")
        utilization_status = approaching_signal.get("utilization_status")
        parsed_amounts: dict[str, Decimal] = {}
        for field_name, raw_value in (
            ("budget_amount", budget_amount),
            ("remaining_amount", remaining_amount),
        ):
            if isinstance(raw_value, str):
                try:
                    parsed = Decimal(raw_value)
                    if parsed < Decimal("0"):
                        errors.append(f"$: {field_name} must be a non-negative exact decimal")
                    else:
                        parsed_amounts[field_name] = parsed
                except Exception:
                    errors.append(f"$: {field_name} must be an exact decimal")
        if "budget_amount" in parsed_amounts and parsed_amounts["budget_amount"] <= Decimal("0"):
            errors.append("$: budget_amount must be greater than zero")
        if "remaining_amount" in parsed_amounts and utilization_status == "at":
            if parsed_amounts["remaining_amount"] != Decimal("0"):
                errors.append("$: at observations must have zero remaining_amount")
        if "remaining_amount" in parsed_amounts and utilization_status == "under":
            if parsed_amounts["remaining_amount"] <= Decimal("0"):
                errors.append("$: under observations must include a positive remaining_amount")
        if "remaining_amount" in parsed_amounts and utilization_status == "over":
            if parsed_amounts["remaining_amount"] != Decimal("0"):
                errors.append("$: over observations must have zero remaining_amount")
        if approaching_signal.get("spend_budget_status") == "published":
            if approaching_signal.get("next_operator_action") != "wait":
                errors.append("$: published spend budgets must wait")
    elif outcome == "rejected":
        if "rejection_reason_code" not in approaching_signal:
            errors.append("$: rejected approaching signals must include rejection_reason_code")
    if "card_pan" in approaching_signal:
        errors.append("$: spend budget approaching signal must not include card_pan")
    if "retained_earnings" in approaching_signal:
        errors.append("$: spend budget approaching signal must not include retained_earnings")
    if "over_amount" in approaching_signal:
        errors.append("$: spend budget approaching signal must not include over_amount")
    return tuple(errors)


def _missing_success_spend_budget_approaching_signal_fields(
    approaching_signal: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay approaching-signal lacks identity."""
    missing: list[str] = []
    for field_name in (
        "spend_budget_id",
        "tenant_reference",
        "billing_account_id",
        "currency_code",
        "budget_amount",
        "remaining_amount",
        "utilization_status",
        "window_started_at",
        "window_ended_at",
        "spend_budget_status",
        "source_payload_hash",
        "spend_budget_contract_version",
    ):
        if field_name not in approaching_signal:
            missing.append(f"$: {outcome} approaching signals must include {field_name}")
    return tuple(missing)


def validate_spend_budget_approaching_signal_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate live approaching-signal presentment plus nested existing envelopes."""
    schema = load_json_schema(
        SPEND_BUDGET_APPROACHING_SIGNAL_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    if "over_amount" in statement:
        errors.append("$: approaching-signal presentment must not include over_amount")
    if "card_pan" in statement:
        errors.append("$: approaching-signal presentment must not include card_pan")
    if "retained_earnings" in statement:
        errors.append("$: approaching-signal presentment must not include retained_earnings")
    if "payload_json" in statement:
        errors.append("$: approaching-signal presentment must not include payload_json")
    approaching_signal = statement.get("approaching_signal")
    spend_budget_id = None
    if isinstance(approaching_signal, Mapping):
        errors.extend(
            validate_spend_budget_approaching_signal(approaching_signal, schemas_directory)
        )
        raw_budget_id = approaching_signal.get("spend_budget_id")
        if isinstance(raw_budget_id, str):
            spend_budget_id = raw_budget_id
    rows = statement.get("webhook_outbox_events")
    if isinstance(rows, list):
        if len(rows) > 1:
            errors.append(
                "$: approaching-signal presentment has at most one spend_budget.approaching row"
            )
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            errors.extend(validate_webhook_outbox_event_presentment(row, schemas_directory))
            event_type = row.get("event_type_code")
            if event_type is not None and event_type != "spend_budget.approaching":
                errors.append(
                    f"$.webhook_outbox_events[{index}]: event_type_code must be spend_budget.approaching"
                )
            source_id = row.get("source_id")
            if (
                spend_budget_id is not None
                and source_id is not None
                and source_id != spend_budget_id
            ):
                errors.append(
                    f"$.webhook_outbox_events[{index}]: source_id must match approaching_signal.spend_budget_id"
                )
    return tuple(errors)


def _missing_success_spend_budget_fields(
    spend_budget: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay budget lacks identity."""
    missing: list[str] = []
    for field_name in (
        "spend_budget_id",
        "tenant_reference",
        "billing_account_id",
        "currency_code",
        "budget_amount",
        "window_started_at",
        "window_ended_at",
        "spend_budget_status",
        "source_payload_hash",
        "published_at",
    ):
        if field_name not in spend_budget:
            missing.append(f"$: {outcome} spend budgets must include {field_name}")
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


FORBIDDEN_ISSUED_INVOICE_FIELDS = (
    "invoice_number",
    "legal_invoice_number",
    "card_pan",
)


def validate_issued_invoice(
    issued_invoice: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate issued-invoice shape plus identity, totals, and numbering bans."""
    schema = load_json_schema(ISSUED_INVOICE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, issued_invoice))
    if not isinstance(issued_invoice, Mapping):
        return tuple(errors)
    errors.extend(_forbidden_issued_invoice_field_errors(issued_invoice))
    outcome = issued_invoice.get("issued_invoice_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_issued_invoice_fields(issued_invoice, str(outcome)))
        errors.extend(_issued_invoice_total_errors(issued_invoice))
        action = issued_invoice.get("next_operator_action")
        if action is not None and action != "collect":
            errors.append("$: issued invoice must collect")
    elif outcome == "rejected":
        if "rejection_reason_code" not in issued_invoice:
            errors.append("$: rejected issued invoices must include rejection_reason_code")
    return tuple(errors)


def validate_issued_invoice_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate issued-invoice presentment shape plus exact money invariants."""
    schema = load_json_schema(ISSUED_INVOICE_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    errors.extend(_forbidden_issued_invoice_field_errors(statement))
    errors.extend(_issued_invoice_total_errors(statement))
    action = statement.get("next_operator_action")
    if action is not None and action != "collect":
        errors.append("$: stored issued invoice must collect")
    return tuple(errors)


def _forbidden_issued_invoice_field_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return diagnostics when a commercial snapshot invents numbering or PAN."""
    errors: list[str] = []
    for field_name in FORBIDDEN_ISSUED_INVOICE_FIELDS:
        if field_name in payload:
            errors.append(f"$: issued invoice must not include {field_name}")
    return tuple(errors)


def _missing_success_issued_invoice_fields(
    issued_invoice: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay snapshot lacks identity."""
    missing: list[str] = []
    for field_name in (
        "issued_invoice_id",
        "invoice_draft_id",
        "rating_run_id",
        "usage_snapshot_hash",
        "currency_code",
        "tax_exclusive_amount",
        "tax_amount",
        "tax_inclusive_amount",
        "issued_invoice_status",
        "issued_at",
        "source_payload_hash",
        "idempotency_key",
        "next_operator_action",
        "issued_invoice_lines",
    ):
        if field_name not in issued_invoice:
            missing.append(f"$: {outcome} issued invoices must include {field_name}")
    return tuple(missing)


def _issued_invoice_total_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return diagnostics when exclusive plus tax does not equal inclusive."""
    exclusive = payload.get("tax_exclusive_amount")
    tax_amount = payload.get("tax_amount")
    inclusive = payload.get("tax_inclusive_amount")
    errors: list[str] = []
    for field_name, value in (
        ("tax_exclusive_amount", exclusive),
        ("tax_amount", tax_amount),
        ("tax_inclusive_amount", inclusive),
    ):
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
                if parsed < Decimal("0"):
                    errors.append(f"$: {field_name} must not be negative")
            except Exception:
                errors.append(f"$: {field_name} must be an exact decimal")
        elif value is not None:
            errors.append(f"$: {field_name} must be an exact decimal")
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
    return tuple(errors)


FORBIDDEN_ISSUED_CREDIT_NOTE_FIELDS = (
    "credit_note_number",
    "legal_credit_note_number",
    "card_pan",
    "issued_credit_note_lines",
)


def validate_issued_credit_note(
    issued_credit_note: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate issued-credit-note shape plus identity, totals, and numbering bans."""
    schema = load_json_schema(ISSUED_CREDIT_NOTE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, issued_credit_note))
    if not isinstance(issued_credit_note, Mapping):
        return tuple(errors)
    errors.extend(_forbidden_issued_credit_note_field_errors(issued_credit_note))
    outcome = issued_credit_note.get("issued_credit_note_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        errors.extend(_missing_success_issued_credit_note_fields(issued_credit_note, str(outcome)))
        errors.extend(_issued_credit_note_total_errors(issued_credit_note))
        action = issued_credit_note.get("next_operator_action")
        if action is not None and action != "wait":
            errors.append("$: issued credit note must wait")
    elif outcome == "rejected":
        if "rejection_reason_code" not in issued_credit_note:
            errors.append("$: rejected issued credit notes must include rejection_reason_code")
    return tuple(errors)


def validate_issued_credit_note_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate issued-credit-note presentment shape plus exact money invariants."""
    schema = load_json_schema(ISSUED_CREDIT_NOTE_PRESENTMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    errors.extend(_forbidden_issued_credit_note_field_errors(statement))
    errors.extend(_issued_credit_note_total_errors(statement))
    action = statement.get("next_operator_action")
    if action is not None and action != "wait":
        errors.append("$: stored issued credit note must wait")
    return tuple(errors)


def validate_credit_note_application(
    application: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate application shape plus apply-only monetary invariants.

    Accepted and replayed applications must carry the issued credit-note
    identity, collection case, invoice draft, currency, exact applied
    amount, and applied timestamp. Rejections must carry a closed reason
    and must not invent legal credit-note numbers.
    """
    schema = load_json_schema(CREDIT_NOTE_APPLICATION_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, application))
    if not isinstance(application, Mapping):
        return tuple(errors)
    outcome = application.get("credit_note_application_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "credit_note_application_id",
            "issued_credit_note_id",
            "collection_case_id",
            "invoice_draft_id",
            "currency_code",
            "applied_amount",
            "applied_at",
        ):
            if field_name not in application:
                errors.append(f"$: {outcome} applications must include {field_name}")
        applied_amount = application.get("applied_amount")
        if isinstance(applied_amount, str):
            try:
                if Decimal(applied_amount) <= Decimal("0"):
                    errors.append("$: applied_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: applied_amount must be an exact decimal")
        elif applied_amount is not None:
            errors.append("$: applied_amount must be an exact decimal")
        if "legal_credit_note_number" in application:
            errors.append("$: applications must not invent legal credit-note numbers")
    elif outcome == "rejected":
        if "rejection_reason_code" not in application:
            errors.append("$: rejected applications must include rejection_reason_code")
        if "legal_credit_note_number" in application:
            errors.append("$: rejected applications must not invent legal numbers")
    return tuple(errors)


def validate_credit_note_application_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate application presentment plus remaining-outstanding invariants."""
    schema = load_json_schema(
        CREDIT_NOTE_APPLICATION_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    applied_amount = statement.get("applied_amount")
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if isinstance(applied_amount, str):
        try:
            if Decimal(applied_amount) <= Decimal("0"):
                errors.append("$: applied_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: applied_amount must be an exact decimal")
    elif applied_amount is not None:
        errors.append("$: applied_amount must be an exact decimal")
    if isinstance(remaining, str):
        try:
            parsed_remaining = Decimal(remaining)
            if parsed_remaining < Decimal("0"):
                errors.append("$: remaining_outstanding_amount must not be negative")
            if parsed_remaining == Decimal("0") and action != "wait":
                errors.append("$: settled applications must wait")
            if parsed_remaining > Decimal("0") and action != "collect":
                errors.append("$: residual applications must collect")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    elif remaining is not None:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    for forbidden_name in (
        "application_payload_hash",
        "credit_note_application_outcome_code",
        "legal_credit_note_number",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: credit-note application presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_collection_case_settlement(
    settlement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate settle-when-zero shape plus exact-zero remaining invariants.

    Accepted and replayed settlements must carry the case, invoice draft,
    currency, exact-zero remaining, and settled timestamp. Rejections must
    carry a closed reason and must not invent a write-off or legal number.
    """
    schema = load_json_schema(COLLECTION_CASE_SETTLEMENT_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, settlement))
    if not isinstance(settlement, Mapping):
        return tuple(errors)
    outcome = settlement.get("collection_case_settlement_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "collection_case_settlement_id",
            "collection_case_id",
            "invoice_draft_id",
            "currency_code",
            "remaining_outstanding_amount",
            "settled_at",
        ):
            if field_name not in settlement:
                errors.append(f"$: {outcome} settlements must include {field_name}")
        remaining = settlement.get("remaining_outstanding_amount")
        if isinstance(remaining, str):
            try:
                if Decimal(remaining) != Decimal("0"):
                    errors.append("$: remaining_outstanding_amount must be exact zero")
            except Exception:
                errors.append("$: remaining_outstanding_amount must be an exact decimal")
        elif remaining is not None:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
        for forbidden_name in (
            "write_off_amount",
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in settlement:
                errors.append(f"$: settlements must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in settlement:
            errors.append("$: rejected settlements must include rejection_reason_code")
        if "write_off_amount" in settlement:
            errors.append("$: rejected settlements must not invent a write-off")
        if "legal_credit_note_number" in settlement:
            errors.append("$: rejected settlements must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown collection_case_settlement_outcome_code")
    return tuple(errors)


def validate_collection_case_settlement_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate settlement presentment plus exact-zero remaining invariants."""
    schema = load_json_schema(
        COLLECTION_CASE_SETTLEMENT_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if remaining is None:
        errors.append("$: remaining_outstanding_amount is required")
    elif isinstance(remaining, str):
        try:
            if Decimal(remaining) != Decimal("0"):
                errors.append("$: remaining_outstanding_amount must be exact zero")
            if Decimal(remaining) == Decimal("0") and action != "wait":
                errors.append("$: settled presentment must wait")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    else:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    for forbidden_name in (
        "write_off_amount",
        "legal_credit_note_number",
        "legal_invoice_number",
        "collection_case_settlement_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: collection-case settlement presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_collection_write_off(
    write_off: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate write-off shape plus exact remaining and amount invariants.

    Accepted and replayed write-offs must carry the case, invoice draft,
    currency, positive write-off amount, exact-zero remaining, and
    written-off timestamp. Rejections must carry a closed reason and must
    not invent a legal number or settlement.
    """
    schema = load_json_schema(COLLECTION_WRITE_OFF_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, write_off))
    if not isinstance(write_off, Mapping):
        return tuple(errors)
    outcome = write_off.get("collection_write_off_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "collection_write_off_id",
            "collection_case_id",
            "invoice_draft_id",
            "currency_code",
            "write_off_amount",
            "remaining_outstanding_amount",
            "written_off_at",
        ):
            if field_name not in write_off:
                errors.append(f"$: {outcome} write-offs must include {field_name}")
        remaining = write_off.get("remaining_outstanding_amount")
        if isinstance(remaining, str):
            try:
                if Decimal(remaining) != Decimal("0"):
                    errors.append("$: remaining_outstanding_amount must be exact zero")
            except Exception:
                errors.append("$: remaining_outstanding_amount must be an exact decimal")
        elif remaining is not None:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
        write_off_amount = write_off.get("write_off_amount")
        if isinstance(write_off_amount, str):
            try:
                if Decimal(write_off_amount) <= Decimal("0"):
                    errors.append("$: write_off_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: write_off_amount must be an exact decimal")
        elif write_off_amount is not None:
            errors.append("$: write_off_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in write_off:
                errors.append(f"$: write-offs must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in write_off:
            errors.append("$: rejected write-offs must include rejection_reason_code")
        if "legal_invoice_number" in write_off:
            errors.append("$: rejected write-offs must not invent legal numbers")
        if "legal_credit_note_number" in write_off:
            errors.append("$: rejected write-offs must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown collection_write_off_outcome_code")
    return tuple(errors)


def validate_collection_write_off_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate write-off presentment plus exact remaining and amount invariants."""
    schema = load_json_schema(
        COLLECTION_WRITE_OFF_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if remaining is None:
        errors.append("$: remaining_outstanding_amount is required")
    elif isinstance(remaining, str):
        try:
            if Decimal(remaining) != Decimal("0"):
                errors.append("$: remaining_outstanding_amount must be exact zero")
            if Decimal(remaining) == Decimal("0") and action != "settle":
                errors.append("$: write-off presentment must settle")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    else:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    write_off_amount = statement.get("write_off_amount")
    if write_off_amount is None:
        errors.append("$: write_off_amount is required")
    elif isinstance(write_off_amount, str):
        try:
            if Decimal(write_off_amount) <= Decimal("0"):
                errors.append("$: write_off_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: write_off_amount must be an exact decimal")
    else:
        errors.append("$: write_off_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "collection_write_off_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: collection write-off presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_collection_dispute(
    dispute: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate hold shape plus exact remaining snapshot invariants.

    Accepted and replayed holds must carry the case, invoice draft,
    currency, non-negative remaining snapshot, and held timestamp.
    Rejections must carry a closed reason and must not invent a legal
    number, journal, or webhook.
    """
    schema = load_json_schema(COLLECTION_DISPUTE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, dispute))
    if not isinstance(dispute, Mapping):
        return tuple(errors)
    outcome = dispute.get("collection_dispute_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "collection_dispute_id",
            "collection_case_id",
            "invoice_draft_id",
            "currency_code",
            "remaining_outstanding_amount",
            "held_at",
        ):
            if field_name not in dispute:
                errors.append(f"$: {outcome} disputes must include {field_name}")
        remaining = dispute.get("remaining_outstanding_amount")
        if isinstance(remaining, str):
            try:
                if Decimal(remaining) < Decimal("0"):
                    errors.append("$: remaining_outstanding_amount must be a non-negative exact decimal")
            except Exception:
                errors.append("$: remaining_outstanding_amount must be an exact decimal")
        elif remaining is not None:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in dispute:
                errors.append(f"$: disputes must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in dispute:
            errors.append("$: rejected disputes must include rejection_reason_code")
        if "legal_invoice_number" in dispute:
            errors.append("$: rejected disputes must not invent legal numbers")
        if "legal_credit_note_number" in dispute:
            errors.append("$: rejected disputes must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown collection_dispute_outcome_code")
    return tuple(errors)


def validate_collection_dispute_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate hold presentment plus exact remaining snapshot invariants."""
    schema = load_json_schema(
        COLLECTION_DISPUTE_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if remaining is None:
        errors.append("$: remaining_outstanding_amount is required")
    elif isinstance(remaining, str):
        try:
            if Decimal(remaining) < Decimal("0"):
                errors.append("$: remaining_outstanding_amount must be a non-negative exact decimal")
            if action != "wait":
                errors.append("$: dispute presentment must wait")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    else:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "collection_dispute_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: collection dispute presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_collection_dispute_release(
    release: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate release shape plus exact remaining snapshot invariants.

    Accepted and replayed releases must carry the dispute, case, invoice
    draft, currency, non-negative remaining snapshot, and released
    timestamp. Rejections must carry a closed reason and must not invent
    a legal number, journal, or webhook.
    """
    schema = load_json_schema(COLLECTION_DISPUTE_RELEASE_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, release))
    if not isinstance(release, Mapping):
        return tuple(errors)
    outcome = release.get("collection_dispute_release_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "collection_dispute_id",
            "collection_case_id",
            "invoice_draft_id",
            "currency_code",
            "remaining_outstanding_amount",
            "released_at",
        ):
            if field_name not in release:
                errors.append(f"$: {outcome} releases must include {field_name}")
        remaining = release.get("remaining_outstanding_amount")
        if isinstance(remaining, str):
            try:
                if Decimal(remaining) < Decimal("0"):
                    errors.append("$: remaining_outstanding_amount must be a non-negative exact decimal")
            except Exception:
                errors.append("$: remaining_outstanding_amount must be an exact decimal")
        elif remaining is not None:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in release:
                errors.append(f"$: dispute releases must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in release:
            errors.append("$: rejected releases must include rejection_reason_code")
        if "legal_invoice_number" in release:
            errors.append("$: rejected releases must not invent legal numbers")
        if "legal_credit_note_number" in release:
            errors.append("$: rejected releases must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown collection_dispute_release_outcome_code")
    return tuple(errors)


def validate_collection_dispute_release_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate release presentment plus exact remaining snapshot invariants."""
    schema = load_json_schema(
        COLLECTION_DISPUTE_RELEASE_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if remaining is None:
        errors.append("$: remaining_outstanding_amount is required")
    elif isinstance(remaining, str):
        try:
            if Decimal(remaining) < Decimal("0"):
                errors.append("$: remaining_outstanding_amount must be a non-negative exact decimal")
            if action != "wait":
                errors.append("$: dispute release presentment must wait")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    else:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "collection_dispute_release_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: collection dispute release presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_issued_invoice_void(
    void_row: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate void shape plus exact voided-amount invariants.

    Accepted and replayed voids must carry the issued invoice, draft,
    currency, positive voided amount, and voided timestamp. Rejections
    must carry a closed reason and must not invent a legal number.
    """
    schema = load_json_schema(ISSUED_INVOICE_VOID_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, void_row))
    if not isinstance(void_row, Mapping):
        return tuple(errors)
    outcome = void_row.get("issued_invoice_void_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "issued_invoice_void_id",
            "issued_invoice_id",
            "invoice_draft_id",
            "currency_code",
            "voided_amount",
            "voided_at",
        ):
            if field_name not in void_row:
                errors.append(f"$: {outcome} voids must include {field_name}")
        remaining = void_row.get("remaining_outstanding_amount")
        if isinstance(remaining, str):
            try:
                if Decimal(remaining) != Decimal("0"):
                    errors.append("$: remaining_outstanding_amount must be exact zero")
            except Exception:
                errors.append("$: remaining_outstanding_amount must be an exact decimal")
        elif remaining is not None:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
        voided_amount = void_row.get("voided_amount")
        if isinstance(voided_amount, str):
            try:
                if Decimal(voided_amount) <= Decimal("0"):
                    errors.append("$: voided_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: voided_amount must be an exact decimal")
        elif voided_amount is not None:
            errors.append("$: voided_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in void_row:
                errors.append(f"$: voids must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in void_row:
            errors.append("$: rejected voids must include rejection_reason_code")
        if "legal_invoice_number" in void_row:
            errors.append("$: rejected voids must not invent legal numbers")
        if "legal_credit_note_number" in void_row:
            errors.append("$: rejected voids must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown issued_invoice_void_outcome_code")
    return tuple(errors)


def validate_issued_invoice_void_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate void presentment plus exact voided-amount invariants."""
    schema = load_json_schema(
        ISSUED_INVOICE_VOID_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if isinstance(remaining, str):
        try:
            if Decimal(remaining) != Decimal("0"):
                errors.append("$: remaining_outstanding_amount must be exact zero")
            if Decimal(remaining) == Decimal("0") and action != "wait":
                errors.append("$: issued-invoice void presentment must wait")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    elif remaining is not None:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    voided_amount = statement.get("voided_amount")
    if voided_amount is None:
        errors.append("$: voided_amount is required")
    elif isinstance(voided_amount, str):
        try:
            if Decimal(voided_amount) <= Decimal("0"):
                errors.append("$: voided_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: voided_amount must be an exact decimal")
    else:
        errors.append("$: voided_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "issued_invoice_void_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: issued-invoice void presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_issued_credit_note_void(
    void_row: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate void shape plus exact voided-amount invariants.

    Accepted and replayed voids must carry the issued credit note, credit,
    draft, currency, positive voided amount, and voided timestamp.
    Rejections must carry a closed reason and must not invent a legal
    number.
    """
    schema = load_json_schema(ISSUED_CREDIT_NOTE_VOID_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, void_row))
    if not isinstance(void_row, Mapping):
        return tuple(errors)
    outcome = void_row.get("issued_credit_note_void_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "issued_credit_note_void_id",
            "issued_credit_note_id",
            "credit_adjustment_id",
            "invoice_draft_id",
            "currency_code",
            "voided_amount",
            "voided_at",
        ):
            if field_name not in void_row:
                errors.append(f"$: {outcome} voids must include {field_name}")
        voided_amount = void_row.get("voided_amount")
        if isinstance(voided_amount, str):
            try:
                if Decimal(voided_amount) <= Decimal("0"):
                    errors.append("$: voided_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: voided_amount must be an exact decimal")
        elif voided_amount is not None:
            errors.append("$: voided_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in void_row:
                errors.append(f"$: voids must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in void_row:
            errors.append("$: rejected voids must include rejection_reason_code")
        if "legal_invoice_number" in void_row:
            errors.append("$: rejected voids must not invent legal numbers")
        if "legal_credit_note_number" in void_row:
            errors.append("$: rejected voids must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown issued_credit_note_void_outcome_code")
    return tuple(errors)


def validate_issued_credit_note_void_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate void presentment plus exact voided-amount invariants."""
    schema = load_json_schema(
        ISSUED_CREDIT_NOTE_VOID_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    voided_amount = statement.get("voided_amount")
    if voided_amount is None:
        errors.append("$: voided_amount is required")
    elif isinstance(voided_amount, str):
        try:
            if Decimal(voided_amount) <= Decimal("0"):
                errors.append("$: voided_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: voided_amount must be an exact decimal")
    else:
        errors.append("$: voided_amount must be an exact decimal")
    action = statement.get("next_operator_action")
    if action is not None and action != "wait":
        errors.append("$: issued-credit-note void presentment must wait")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "issued_credit_note_void_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: issued-credit-note void presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_unapplied_cash(
    leftover: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate leftover shape plus exact parked-amount invariants.

    Accepted and replayed leftover must carry the receipt, leftover
    amount, receipt snapshots, parked timestamp, and hash. Rejections
    must carry a closed reason and must not invent a legal number.
    """
    schema = load_json_schema(UNAPPLIED_CASH_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, leftover))
    if not isinstance(leftover, Mapping):
        return tuple(errors)
    outcome = leftover.get("unapplied_cash_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "unapplied_cash_id",
            "tenant_reference",
            "payment_receipt_id",
            "payment_intent_id",
            "collection_case_id",
            "currency_code",
            "unapplied_amount",
            "received_amount",
            "applied_amount",
            "unapplied_cash_status",
            "parked_at",
            "source_payload_hash",
        ):
            if field_name not in leftover:
                errors.append(f"$: {outcome} unapplied cash must include {field_name}")
        unapplied_amount = leftover.get("unapplied_amount")
        if isinstance(unapplied_amount, str):
            try:
                if Decimal(unapplied_amount) <= Decimal("0"):
                    errors.append("$: unapplied_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: unapplied_amount must be an exact decimal")
        elif unapplied_amount is not None:
            errors.append("$: unapplied_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in leftover:
                errors.append(f"$: unapplied cash must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in leftover:
            errors.append("$: rejected unapplied cash must include rejection_reason_code")
        if "legal_invoice_number" in leftover:
            errors.append("$: rejected unapplied cash must not invent legal numbers")
        if "legal_credit_note_number" in leftover:
            errors.append("$: rejected unapplied cash must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown unapplied_cash_outcome_code")
    return tuple(errors)


def validate_unapplied_cash_application(
    application: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate leftover-apply shape plus exact applied-amount invariants.

    Accepted and replayed applications must carry leftover, case, receipt,
    invoice draft, currency, exact applied amount, remaining, and applied
    timestamp. Rejections must carry a closed reason and must not invent
    a legal number.
    """
    schema = load_json_schema(UNAPPLIED_CASH_APPLICATION_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, application))
    if not isinstance(application, Mapping):
        return tuple(errors)
    outcome = application.get("unapplied_cash_application_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "unapplied_cash_application_id",
            "unapplied_cash_id",
            "collection_case_id",
            "payment_receipt_id",
            "invoice_draft_id",
            "currency_code",
            "applied_amount",
            "remaining_outstanding_amount",
            "applied_at",
        ):
            if field_name not in application:
                errors.append(f"$: {outcome} applications must include {field_name}")
        applied_amount = application.get("applied_amount")
        if isinstance(applied_amount, str):
            try:
                if Decimal(applied_amount) <= Decimal("0"):
                    errors.append("$: applied_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: applied_amount must be an exact decimal")
        elif applied_amount is not None:
            errors.append("$: applied_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in application:
                errors.append(f"$: applications must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in application:
            errors.append("$: rejected applications must include rejection_reason_code")
        if "legal_invoice_number" in application:
            errors.append("$: rejected applications must not invent legal numbers")
        if "legal_credit_note_number" in application:
            errors.append("$: rejected applications must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown unapplied_cash_application_outcome_code")
    return tuple(errors)


def validate_unapplied_cash_application_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate leftover-apply presentment plus remaining-outstanding invariants."""
    schema = load_json_schema(
        UNAPPLIED_CASH_APPLICATION_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    applied_amount = statement.get("applied_amount")
    remaining = statement.get("remaining_outstanding_amount")
    action = statement.get("next_operator_action")
    if applied_amount is None:
        errors.append("$: applied_amount is required")
    elif isinstance(applied_amount, str):
        try:
            if Decimal(applied_amount) <= Decimal("0"):
                errors.append("$: applied_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: applied_amount must be an exact decimal")
    else:
        errors.append("$: applied_amount must be an exact decimal")
    if remaining is None:
        errors.append("$: remaining_outstanding_amount is required")
    elif isinstance(remaining, str):
        try:
            parsed_remaining = Decimal(remaining)
            if parsed_remaining < Decimal("0"):
                errors.append("$: remaining_outstanding_amount must not be negative")
            if parsed_remaining == Decimal("0") and action not in ("settle", "wait"):
                errors.append("$: zero remaining applications must settle or wait")
            if parsed_remaining > Decimal("0") and action != "collect":
                errors.append("$: residual applications must collect")
        except Exception:
            errors.append("$: remaining_outstanding_amount must be an exact decimal")
    else:
        errors.append("$: remaining_outstanding_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "unapplied_cash_application_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: unapplied cash application presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_unapplied_cash_refund(
    refund: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate leftover-refund shape plus exact refund-amount invariants.

    Accepted and replayed refunds must carry leftover, receipt, currency,
    exact refund amount, parked leftover snapshot, and refunded timestamp.
    Rejections must carry a closed reason and must not invent a legal number.
    """
    schema = load_json_schema(UNAPPLIED_CASH_REFUND_SCHEMA_NAME, schemas_directory)
    errors = list(validate_schema_instance(schema, refund))
    if not isinstance(refund, Mapping):
        return tuple(errors)
    outcome = refund.get("unapplied_cash_refund_outcome_code")
    if outcome == "accepted" or outcome == "duplicate_replay":
        for field_name in (
            "unapplied_cash_refund_id",
            "unapplied_cash_id",
            "payment_receipt_id",
            "payment_intent_id",
            "collection_case_id",
            "currency_code",
            "refund_amount",
            "unapplied_amount",
            "refunded_at",
        ):
            if field_name not in refund:
                errors.append(f"$: {outcome} refunds must include {field_name}")
        refund_amount = refund.get("refund_amount")
        if isinstance(refund_amount, str):
            try:
                if Decimal(refund_amount) <= Decimal("0"):
                    errors.append("$: refund_amount must be a positive exact decimal")
            except Exception:
                errors.append("$: refund_amount must be an exact decimal")
        elif refund_amount is not None:
            errors.append("$: refund_amount must be an exact decimal")
        for forbidden_name in (
            "legal_credit_note_number",
            "legal_invoice_number",
            "card_pan",
        ):
            if forbidden_name in refund:
                errors.append(f"$: refunds must not include {forbidden_name}")
    elif outcome == "rejected":
        if "rejection_reason_code" not in refund:
            errors.append("$: rejected refunds must include rejection_reason_code")
        if "legal_invoice_number" in refund:
            errors.append("$: rejected refunds must not invent legal numbers")
        if "legal_credit_note_number" in refund:
            errors.append("$: rejected refunds must not invent legal numbers")
    else:
        if outcome is not None:
            errors.append("$: unknown unapplied_cash_refund_outcome_code")
    return tuple(errors)


def validate_unapplied_cash_refund_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:
    """Validate leftover-refund presentment plus exact refund-amount invariants."""
    schema = load_json_schema(
        UNAPPLIED_CASH_REFUND_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    if action != "wait":
        errors.append("$: unapplied cash refund presentment must wait")
    refund_amount = statement.get("refund_amount")
    if refund_amount is None:
        errors.append("$: refund_amount is required")
    elif isinstance(refund_amount, str):
        try:
            if Decimal(refund_amount) <= Decimal("0"):
                errors.append("$: refund_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: refund_amount must be an exact decimal")
    else:
        errors.append("$: refund_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "unapplied_cash_refund_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: unapplied cash refund presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def validate_unapplied_cash_presentment(
    statement: Any, schemas_directory: Path | None = None
) -> tuple[str, ...]:

    """Validate leftover presentment plus exact parked-amount invariants."""
    schema = load_json_schema(
        UNAPPLIED_CASH_PRESENTMENT_SCHEMA_NAME, schemas_directory
    )
    errors = list(validate_schema_instance(schema, statement))
    if not isinstance(statement, Mapping):
        return tuple(errors)
    action = statement.get("next_operator_action")
    if action != "wait":
        errors.append("$: unapplied cash presentment must wait")
    if "apply_to_collection_case_id" in statement:
        errors.append("$: unapplied cash presentment must not apply leftover")
    unapplied_amount = statement.get("unapplied_amount")
    if unapplied_amount is None:
        errors.append("$: unapplied_amount is required")
    elif isinstance(unapplied_amount, str):
        try:
            if Decimal(unapplied_amount) <= Decimal("0"):
                errors.append("$: unapplied_amount must be a positive exact decimal")
        except Exception:
            errors.append("$: unapplied_amount must be an exact decimal")
    else:
        errors.append("$: unapplied_amount must be an exact decimal")
    for forbidden_name in (
        "legal_credit_note_number",
        "legal_invoice_number",
        "unapplied_cash_outcome_code",
        "card_pan",
    ):
        if forbidden_name in statement:
            errors.append(
                f"$: unapplied cash presentment must not include {forbidden_name}"
            )
    return tuple(errors)


def _forbidden_issued_credit_note_field_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return diagnostics when a commercial snapshot invents numbering or PAN."""
    errors: list[str] = []
    for field_name in FORBIDDEN_ISSUED_CREDIT_NOTE_FIELDS:
        if field_name in payload:
            errors.append(f"$: issued credit note must not include {field_name}")
    return tuple(errors)


def _missing_success_issued_credit_note_fields(
    issued_credit_note: Mapping[str, Any], outcome: str
) -> tuple[str, ...]:
    """Return semantic errors when an accepted or replay snapshot lacks identity."""
    missing: list[str] = []
    for field_name in (
        "issued_credit_note_id",
        "credit_adjustment_id",
        "invoice_draft_id",
        "currency_code",
        "tax_exclusive_amount",
        "tax_amount",
        "tax_inclusive_amount",
        "issued_credit_note_status",
        "issued_at",
        "source_payload_hash",
        "credit_adjustment_source_payload_hash",
        "credit_adjustment_contract_version",
        "credit_reason_code",
        "idempotency_key",
        "next_operator_action",
    ):
        if field_name not in issued_credit_note:
            missing.append(f"$: {outcome} issued credit notes must include {field_name}")
    return tuple(missing)


def _issued_credit_note_total_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return diagnostics when exclusive plus tax does not equal inclusive."""
    exclusive = payload.get("tax_exclusive_amount")
    tax_amount = payload.get("tax_amount")
    inclusive = payload.get("tax_inclusive_amount")
    errors: list[str] = []
    for field_name, value in (
        ("tax_exclusive_amount", exclusive),
        ("tax_amount", tax_amount),
        ("tax_inclusive_amount", inclusive),
    ):
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
                if parsed < Decimal("0"):
                    errors.append(f"$: {field_name} must not be negative")
            except Exception:
                errors.append(f"$: {field_name} must be an exact decimal")
        elif value is not None:
            errors.append(f"$: {field_name} must be an exact decimal")
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
