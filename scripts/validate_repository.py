"""Validate the repository's portable contracts without network access.

The validator deliberately implements only the JSON Schema Draft 2020-12
keywords used by this repository.  Runtime consumers remain free to use any
standards-compliant JSON Schema implementation; this module exists so exact-head
CI can verify the checked-in contracts without downloading transitive packages.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID


DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
SchemaNode = Mapping[str, Any] | bool
REQUIRED_FILES = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    "LICENSE",
    "docs/PRD.md",
    "docs/TRD.md",
    "docs/ARCHITECTURE.md",
    "docs/DATA_MODEL.md",
    "docs/ACCOUNTING_BOUNDARY.md",
    "docs/adr/0001-commercial-authority.md",
    "docs/adr/0002-accounting-boundary.md",
    "docs/adr/0003-usage-ingestion-idempotency.md",
    "docs/adr/0004-deterministic-windowed-rating.md",
    "docs/adr/0005-invoice-draft-from-rating.md",
    "docs/adr/0006-journal-proposal-from-invoice-draft.md",
    "docs/adr/0007-collection-case-from-invoice-draft.md",
    "docs/adr/0008-payment-intent-from-collection-case.md",
    "docs/adr/0009-payment-receipt-from-payment-intent.md",
    "docs/adr/0010-cash-journal-from-payment-receipt.md",
    "docs/adr/0011-http-accept-surface.md",
    "docs/adr/0012-journal-proposal-query.md",
    "docs/adr/0013-posting-receipt-observation.md",
    "docs/adr/0014-credit-adjustment-from-invoice-draft.md",
    "docs/adr/0015-versioned-rate-card-catalog.md",
    "docs/adr/0016-tax-assessment-on-invoice-draft.md",
    "docs/adr/0017-tax-payable-unwind-on-credit.md",
    "docs/adr/0018-invoice-draft-presentment.md",
    "docs/adr/0019-tenant-api-credentials.md",
    "docs/adr/0020-operator-presentment-storybook.md",
    "docs/adr/0021-webhook-outbox.md",
    "docs/adr/0022-ais-outbox-drain.md",
    "docs/adr/0023-collection-case-presentment.md",
    "docs/adr/0024-payment-intent-http.md",
    "docs/adr/0025-payment-receipt-http.md",
    "docs/adr/0026-cash-journal-on-receipt-accept.md",
    "docs/adr/0027-credit-adjustment-presentment.md",
    "docs/adr/0028-rate-card-presentment.md",
    "docs/adr/0029-usage-event-presentment.md",
    "docs/adr/0030-rating-run-presentment.md",
    "docs/adr/0031-tax-assessment-presentment.md",
    "docs/adr/0032-posting-receipt-observation-presentment.md",
    "docs/adr/0033-webhook-delivery-presentment.md",
    "docs/adr/0034-tenant-api-credential-presentment.md",
    "docs/adr/0035-webhook-subscription-presentment.md",
    "docs/adr/0036-dunning-event-presentment.md",
    "docs/adr/0037-webhook-outbox-event-presentment.md",
    "docs/adr/0038-issued-invoice-from-draft.md",
    "docs/adr/0039-invoice-issued-webhook.md",
    "docs/adr/0040-issued-credit-note-from-adjustment.md",
    "docs/adr/0041-credit-note-issued-webhook.md",
    "docs/adr/0042-credit-note-application-to-collection-case.md",
    "docs/adr/0043-collection-case-settle-when-zero.md",
    "docs/adr/0044-collection-settled-webhook.md",
    "docs/adr/0045-credit-note-applied-webhook.md",
    "docs/adr/0046-collection-write-off.md",
    "docs/adr/0047-write-off-recorded-webhook.md",
    "docs/adr/0048-write-off-journal-from-collection-write-off.md",
    "docs/adr/0049-credit-journal-from-credit-adjustment.md",
    "docs/adr/0050-collection-aging-presentment.md",
    "docs/adr/0051-unapplied-cash-from-payment-receipt.md",
    "docs/adr/0052-unapplied-cash-application-to-collection-case.md",
    "docs/adr/0053-unapplied-cash-applied-webhook.md",
    "docs/adr/0054-unapplied-cash-refund.md",
    "docs/adr/0055-refund-recorded-webhook.md",
    "docs/adr/0056-refund-journal-from-unapplied-cash-refund.md",
    "docs/adr/0057-unapplied-cash-journal-from-parked-leftover.md",
    "docs/adr/0058-unapplied-cash-application-journal.md",
    "docs/adr/0059-account-statement-presentment.md",
    "docs/adr/0060-issued-invoice-void.md",
    "docs/adr/0061-invoice-voided-webhook.md",
    "docs/adr/0062-void-journal-from-issued-invoice-void.md",
    "docs/adr/0063-collection-dispute-hold.md",
    "docs/adr/0064-collection-dispute-release.md",
    "docs/adr/0065-dispute-held-webhook.md",
    "docs/adr/0066-dispute-released-webhook.md",
    "docs/adr/0067-issued-invoice-presentment-tax-assessment.md",
    "docs/adr/0068-issued-credit-note-presentment-tax-assessment.md",
    "docs/adr/0069-issued-credit-note-void.md",
    "docs/adr/0070-credit-note-voided-webhook.md",
    "docs/adr/0071-credit-note-void-journal-from-issued-credit-note-void.md",
    "docs/adr/0072-account-statement-void-totals.md",
    "docs/adr/0073-operator-account-statement-storybook.md",
    "docs/adr/0074-rated-spend-presentment.md",
    "docs/adr/0075-rated-spend-group-by-project.md",
    "docs/adr/0076-rated-spend-group-by-credential.md",
    "docs/adr/0077-rated-spend-group-by-principal.md",
    "docs/adr/0078-rated-spend-group-by-cost-center.md",
    "docs/adr/0079-spend-budget-publish.md",
    "docs/adr/0080-postgres-usage-ingestion.md",
    "docs/adr/0081-postgres-commercial-vertical-slice.md",
    "docs/adr/0082-spend-budget-evaluation.md",
    "docs/adr/0083-journal-compose-six-place-scale.md",
    "docs/adr/0084-spend-budget-published-webhook.md",
    "docs/adr/0085-operator-spend-budget-storybook.md",
    "docs/adr/0086-billing-account-budget-status.md",
    "docs/adr/0087-operator-rated-spend-storybook.md",
    "docs/adr/0088-operator-budget-status-storybook.md",
    "docs/adr/0089-postgres-spend-budget.md",
    "docs/adr/0090-spend-budget-over-webhook.md",
    "docs/adr/0091-operator-spend-budget-over-storybook.md",
    "docs/adr/0092-spend-budget-over-signal-presentment.md",
    "docs/adr/0093-spend-budget-approaching-webhook.md",
    "docs/adr/0094-operator-spend-budget-approaching-storybook.md",
    "docs/adr/0095-spend-budget-approaching-signal-presentment.md",
    "docs/adr/0096-postgres-issued-credit-note.md",
    "docs/adr/0097-postgres-issued-credit-note-void.md",
    "docs/adr/0098-postgres-credit-note-application.md",
    "docs/adr/0099-postgres-issued-invoice-void.md",
    "docs/adr/0100-postgres-unapplied-cash.md",
    "docs/adr/0101-postgres-unapplied-cash-application.md",
    "docs/adr/0102-postgres-unapplied-cash-refund.md",
    "docs/adr/0103-postgres-collection-dispute.md",
    "docs/adr/0104-operator-collection-dispute-storybook.md",
    "docs/adr/0105-operator-unapplied-cash-storybook.md",
    "docs/adr/0106-operator-issued-credit-note-void-storybook.md",
    "docs/adr/0107-operator-collection-write-off-storybook.md",
    "docs/adr/0108-operator-collection-case-settlement-storybook.md",
    "docs/adr/0109-operator-issued-invoice-void-storybook.md",
    "docs/adr/0110-operator-journal-proposal-storybook.md",
    "docs/adr/0111-postgres-write-off-journal.md",
    "docs/adr/0112-postgres-unapplied-cash-journal.md",
    "docs/STORYBOOK.md",
    "docs/SECURITY.md",
    "docs/doctoring/REFERENCES.md",
    "docs/doctoring/STANDARD_TRACEABILITY.md",
    "schemas/usage-event.schema.json",
    "schemas/provider-capability.schema.json",
    "schemas/accounting-journal-proposal.schema.json",
    "schemas/usage-ingestion-receipt.schema.json",
    "schemas/rating-run.schema.json",
    "schemas/invoice-draft.schema.json",
    "schemas/invoice-draft-presentment.schema.json",
    "schemas/collection-case.schema.json",
    "schemas/payment-intent.schema.json",
    "schemas/payment-receipt.schema.json",
    "schemas/credit-adjustment.schema.json",
    "schemas/rate-card.schema.json",
    "schemas/tax-rate.schema.json",
    "schemas/tax-assessment.schema.json",
    "schemas/tenant-api-credential.schema.json",
    "schemas/webhook-subscription.schema.json",
    "schemas/webhook-delivery.schema.json",
    "schemas/ais-outbox-drain.schema.json",
    "schemas/collection-case-presentment.schema.json",
    "schemas/collection-aging-presentment.schema.json",
    "schemas/account-statement-presentment.schema.json",
    "schemas/rated-spend-presentment.schema.json",
    "schemas/spend-budget.schema.json",
    "schemas/spend-budget-over-signal.schema.json",
    "schemas/spend-budget-over-signal-presentment.schema.json",
    "schemas/spend-budget-approaching-signal.schema.json",
    "schemas/spend-budget-approaching-signal-presentment.schema.json",
    "schemas/spend-budget-presentment.schema.json",
    "schemas/spend-budget-evaluation-presentment.schema.json",
    "schemas/billing-account-budget-status-presentment.schema.json",
    "schemas/payment-intent-presentment.schema.json",
    "schemas/payment-receipt-presentment.schema.json",
    "schemas/credit-adjustment-presentment.schema.json",
    "schemas/rate-card-presentment.schema.json",
    "schemas/usage-event-presentment.schema.json",
    "schemas/rating-run-presentment.schema.json",
    "schemas/tax-assessment-presentment.schema.json",
    "schemas/posting-receipt-observation-presentment.schema.json",
    "schemas/webhook-delivery-presentment.schema.json",
    "schemas/tenant-api-credential-presentment.schema.json",
    "schemas/webhook-subscription-presentment.schema.json",
    "schemas/dunning-event-presentment.schema.json",
    "schemas/webhook-outbox-event-presentment.schema.json",
    "schemas/issued-invoice.schema.json",
    "schemas/issued-invoice-presentment.schema.json",
    "schemas/issued-invoice-void.schema.json",
    "schemas/issued-invoice-void-presentment.schema.json",
    "schemas/issued-credit-note.schema.json",
    "schemas/issued-credit-note-presentment.schema.json",
    "schemas/issued-credit-note-void.schema.json",
    "schemas/issued-credit-note-void-presentment.schema.json",
    "schemas/credit-note-application.schema.json",
    "schemas/credit-note-application-presentment.schema.json",
    "schemas/collection-case-settlement.schema.json",
    "schemas/collection-case-settlement-presentment.schema.json",
    "schemas/collection-write-off.schema.json",
    "schemas/collection-write-off-presentment.schema.json",
    "schemas/collection-dispute.schema.json",
    "schemas/collection-dispute-presentment.schema.json",
    "schemas/collection-dispute-release.schema.json",
    "schemas/collection-dispute-release-presentment.schema.json",
    "schemas/unapplied-cash.schema.json",
    "schemas/unapplied-cash-presentment.schema.json",
    "schemas/unapplied-cash-application.schema.json",
    "schemas/unapplied-cash-application-presentment.schema.json",
    "schemas/unapplied-cash-refund.schema.json",
    "schemas/unapplied-cash-refund-presentment.schema.json",
    "schemas/consumed/accounting-posting-receipt.schema.json",
    "schemas/consumed/README.md",
    "database/migrations/0001_initial_billing_core.sql",
    "database/migrations/0002_usage_event_idempotency.sql",
    "database/migrations/0003_rating_run.sql",
    "database/migrations/0004_invoice_draft.sql",
    "database/migrations/0005_journal_proposal.sql",
    "database/migrations/0006_collection_case.sql",
    "database/migrations/0007_payment_intent.sql",
    "database/migrations/0008_payment_receipt.sql",
    "database/migrations/0009_cash_journal_proposal.sql",
    "database/migrations/0010_posting_receipt_observation.sql",
    "database/migrations/0011_credit_adjustment.sql",
    "database/migrations/0012_rate_card_catalog.sql",
    "database/migrations/0013_tax_assessment.sql",
    "database/migrations/0014_credit_tax_unwind.sql",
    "database/migrations/0015_tenant_api_credential.sql",
    "database/migrations/0016_webhook_outbox.sql",
    "database/migrations/0017_issued_invoice.sql",
    "database/migrations/0018_issued_credit_note.sql",
    "database/migrations/0019_credit_note_application.sql",
    "database/migrations/0020_collection_case_settlement.sql",
    "database/migrations/0021_collection_write_off.sql",
    "database/migrations/0022_write_off_journal_proposal.sql",
    "database/migrations/0023_unapplied_cash.sql",
    "database/migrations/0024_unapplied_cash_application.sql",
    "database/migrations/0025_unapplied_cash_refund.sql",
    "database/migrations/0026_refund_journal_proposal.sql",
    "database/migrations/0027_unapplied_cash_journal_proposal.sql",
    "database/migrations/0028_unapplied_cash_application_journal_proposal.sql",
    "database/migrations/0029_issued_invoice_void.sql",
    "database/migrations/0030_void_journal_proposal.sql",
    "database/migrations/0031_collection_dispute.sql",
    "database/migrations/0032_collection_dispute_release.sql",
    "database/migrations/0033_issued_credit_note_void.sql",
    "database/migrations/0034_credit_note_void_journal_proposal.sql",
    "database/migrations/0035_spend_budget.sql",
    "database/migrations/0036_persistence_integrity_constraints.sql",
    "database/migrations/0037_catalog_reference_identity.sql",
    "database/migrations/0038_postgres_rating_vertical_slice.sql",
    "database/migrations/0039_spend_budget_status.sql",
    "metering_billing/__init__.py",
    "metering_billing/usage_ingestion.py",
    "metering_billing/usage_rating.py",
    "metering_billing/invoice_draft.py",
    "metering_billing/accounting_export.py",
    "metering_billing/collection_case.py",
    "metering_billing/payment_intent.py",
    "metering_billing/payment_settlement.py",
    "metering_billing/http_app.py",
    "metering_billing/posting_receipt.py",
    "metering_billing/credit_adjustment.py",
    "metering_billing/spend_budget.py",
    "metering_billing/spend_budget_presentment.py",
    "metering_billing/spend_budget_evaluation_presentment.py",
    "metering_billing/spend_budget_over_signal.py",
    "metering_billing/spend_budget_over_signal_presentment.py",
    "metering_billing/spend_budget_approaching_signal.py",
    "metering_billing/spend_budget_approaching_signal_presentment.py",
    "metering_billing/rate_card.py",
    "metering_billing/tax_rate.py",
    "metering_billing/tax_assessment.py",
    "metering_billing/tenant_api_credential.py",
    "metering_billing/webhook_outbox.py",
    "metering_billing/ais_outbox_drain.py",
    "metering_billing/collection_case_presentment.py",
    "metering_billing/collection_aging_presentment.py",
    "metering_billing/payment_intent_presentment.py",
    "metering_billing/payment_receipt_presentment.py",
    "metering_billing/credit_adjustment_presentment.py",
    "metering_billing/rate_card_presentment.py",
    "metering_billing/usage_event_presentment.py",
    "metering_billing/rating_run_presentment.py",
    "metering_billing/tax_assessment_presentment.py",
    "metering_billing/posting_receipt_observation_presentment.py",
    "metering_billing/webhook_delivery_presentment.py",
    "metering_billing/tenant_api_credential_presentment.py",
    "metering_billing/webhook_subscription_presentment.py",
    "metering_billing/dunning_event_presentment.py",
    "metering_billing/webhook_outbox_event_presentment.py",
    "metering_billing/issued_invoice.py",
    "metering_billing/issued_invoice_presentment.py",
    "metering_billing/issued_invoice_void.py",
    "metering_billing/issued_invoice_void_presentment.py",
    "metering_billing/issued_credit_note.py",
    "metering_billing/issued_credit_note_presentment.py",
    "metering_billing/issued_credit_note_void.py",
    "metering_billing/issued_credit_note_void_presentment.py",
    "metering_billing/credit_note_application.py",
    "metering_billing/credit_note_application_presentment.py",
    "metering_billing/collection_case_settlement.py",
    "metering_billing/collection_case_settlement_presentment.py",
    "metering_billing/collection_write_off.py",
    "metering_billing/collection_write_off_presentment.py",
    "metering_billing/collection_dispute.py",
    "metering_billing/collection_dispute_presentment.py",
    "metering_billing/unapplied_cash.py",
    "metering_billing/unapplied_cash_presentment.py",
    "metering_billing/unapplied_cash_application.py",
    "metering_billing/unapplied_cash_application_presentment.py",
    "metering_billing/unapplied_cash_refund.py",
    "metering_billing/unapplied_cash_refund_presentment.py",
    "metering_billing/contracts.py",
    "operator_console/package.json",
    "operator_console/src/index.js",
    "operator_console/tokens/design_tokens.json",
    "operator_console/fixtures/taxed_partial_credit.json",
    "scripts/migrate_postgres.py",
    "requirements-quality.txt",
    "requirements-runtime.txt",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
)
ACTION_REFERENCE_PATTERN = re.compile(
    r"\buses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)@([^\s#]+)"
)
FULL_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER_PATTERN = re.compile(
    r"\b(" + "|".join(("TO" + "DO", "T" + "BD", "FIX" + "ME")) + r")\b"
)
SNAKE_CASE_TWO_WORD_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
SCHEMA_NAME_PATTERN = re.compile(
    r"\bCREATE\s+SCHEMA(?:\s+IF\s+NOT\s+EXISTS)?\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)
COLUMN_NAME_PATTERN = re.compile(
    r"(?:^\s+|ADD\s+COLUMN\s+)([a-zA-Z_][a-zA-Z0-9_]*)\s+"
    r"(?:uuid|text|timestamptz|timestamp|integer|bigint|numeric|date|boolean)\b",
    re.IGNORECASE | re.MULTILINE,
)
TABLE_NAME_PATTERN = re.compile(
    r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"(?:(?:[a-zA-Z_][a-zA-Z0-9_]*)\.)?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


def find_mutable_action_references(text: str) -> tuple[str, ...]:
    """Return GitHub Action references that are not pinned to a full SHA."""
    references = {
        f"{match.group(1)}@{match.group(2)}"
        for match in ACTION_REFERENCE_PATTERN.finditer(text)
        if FULL_COMMIT_PATTERN.fullmatch(match.group(2)) is None
    }
    return tuple(sorted(references))


def find_placeholder_tokens(text: str) -> tuple[str, ...]:
    """Return unresolved implementation placeholder tokens in *text*."""
    return tuple(sorted({match.group(1) for match in PLACEHOLDER_PATTERN.finditer(text)}))


def validate_sql_object_names(sql_text: str) -> tuple[str, ...]:
    """Require created PostgreSQL schemas and tables to use two-word snake case."""
    errors: list[str] = []
    for schema_name in SCHEMA_NAME_PATTERN.findall(sql_text):
        if SNAKE_CASE_TWO_WORD_PATTERN.fullmatch(schema_name) is None:
            errors.append(
                f"schema name must contain at least two snake_case words: {schema_name}"
            )
    for table_name in TABLE_NAME_PATTERN.findall(sql_text):
        if SNAKE_CASE_TWO_WORD_PATTERN.fullmatch(table_name) is None:
            errors.append(
                f"table name must contain at least two snake_case words: {table_name}"
            )
    for column_name in COLUMN_NAME_PATTERN.findall(sql_text):
        if SNAKE_CASE_TWO_WORD_PATTERN.fullmatch(column_name) is None:
            errors.append(
                f"column name must contain at least two snake_case words: {column_name}"
            )
    return tuple(errors)


def validate_schema_instance(
    schema: Mapping[str, Any], instance: Any
) -> tuple[str, ...]:
    """Validate *instance* against the Draft 2020-12 subset used in this repo."""
    return tuple(_validate_node(schema, schema, instance, "$"))


def validate_accounting_journal_proposal(
    schema: Mapping[str, Any], proposal: Mapping[str, Any]
) -> tuple[str, ...]:
    """Validate schema shape plus balanced, uniquely numbered journal lines."""
    errors = list(validate_schema_instance(schema, proposal))
    if errors:
        return tuple(errors)

    lines = proposal["lines"]
    line_numbers = [line["line_number"] for line in lines]
    if len(set(line_numbers)) != len(line_numbers):
        errors.append("$: journal line numbers must be unique")

    debit_total = sum(Decimal(line["debit_amount"]) for line in lines)
    credit_total = sum(Decimal(line["credit_amount"]) for line in lines)
    if debit_total != credit_total:
        errors.append("$: debit and credit totals must balance")

    postable_quantum = Decimal("0.000001")
    for line in lines:
        for field_name in ("debit_amount", "credit_amount"):
            amount = Decimal(line[field_name])
            if amount != amount.quantize(postable_quantum):
                errors.append(
                    f"$.lines: {field_name} cannot exceed six fractional digits"
                )
    return tuple(errors)


def validate_repository(root: Path) -> tuple[str, ...]:
    """Return every deterministic repository-contract violation below *root*."""
    errors: list[str] = []
    for relative_path in REQUIRED_FILES:
        if not (root / relative_path).is_file():
            errors.append(f"missing required file: {relative_path}")

    schema_identifiers: set[str] = set()
    schemas_directory = root / "schemas"
    if schemas_directory.is_dir():
        for schema_path in sorted(schemas_directory.glob("*.schema.json")):
            errors.extend(_validate_schema_file(schema_path, schema_identifiers))

    migrations_directory = root / "database/migrations"
    if migrations_directory.is_dir():
        for migration_path in sorted(migrations_directory.glob("*.sql")):
            sql_text = migration_path.read_text(encoding="utf-8")
            relative_path = migration_path.relative_to(root).as_posix()
            for sql_error in validate_sql_object_names(sql_text):
                errors.append(f"{relative_path}: {sql_error}")
            if "provider_customer_id" in sql_text or "stripe_customer_id" in sql_text:
                errors.append(
                    f"{relative_path}: provider-specific identifiers must remain in mapping tables"
                )

    requirements_path = root / "requirements-quality.txt"
    if requirements_path.is_file():
        requirements_text = requirements_path.read_text(encoding="utf-8")
        if "--hash=sha256:" not in requirements_text:
            errors.append("quality dependencies must be hash locked")

    runtime_requirements_path = root / "requirements-runtime.txt"
    if runtime_requirements_path.is_file():
        runtime_requirements_text = runtime_requirements_path.read_text(encoding="utf-8")
        if "--hash=sha256:" not in runtime_requirements_text:
            errors.append("runtime dependencies must be hash locked")

    for file_path in _iter_contract_files(root):
        text = file_path.read_text(encoding="utf-8")
        relative_path = file_path.relative_to(root).as_posix()
        for token in find_placeholder_tokens(text):
            errors.append(f"unresolved placeholder in {relative_path}: {token}")
        if file_path.suffix in {".yml", ".yaml"}:
            for reference in find_mutable_action_references(text):
                errors.append(
                    f"mutable GitHub Action reference in {relative_path}: {reference}"
                )

    return tuple(errors)


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate a supplied repository root and print actionable diagnostics."""
    supplied_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    root = Path(supplied_arguments[0]).resolve() if supplied_arguments else Path.cwd()
    errors = validate_repository(root)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"repository contracts valid: {root}")
    return 0


def _iter_contract_files(root: Path) -> tuple[Path, ...]:
    """Return text contract files while excluding test fixtures and VCS state."""
    included_suffixes = {".json", ".md", ".py", ".sql", ".toml", ".yaml", ".yml"}
    files = []
    for file_path in root.rglob("*"):
        relative_parts = file_path.relative_to(root).parts
        if not file_path.is_file() or file_path.suffix not in included_suffixes:
            continue
        if any(
            part in {
                ".git",
                ".venv",
                "__pycache__",
                "tests",
                "node_modules",
                "storybook-static",
            }
            for part in relative_parts
        ):
            continue
        files.append(file_path)
    return tuple(sorted(files))


def _validate_schema_file(schema_path: Path, identifiers: set[str]) -> tuple[str, ...]:
    """Validate one checked-in JSON Schema's root metadata and identity."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return (f"invalid JSON in {schema_path.name}: {error.msg}",)
    errors: list[str] = []
    if schema.get("$schema") != DRAFT_2020_12:
        errors.append(f"schema must declare Draft 2020-12: {schema_path.name}")
    schema_identifier = schema.get("$id")
    if not isinstance(schema_identifier, str) or not schema_identifier.startswith("https://"):
        errors.append(f"schema must have an HTTPS $id: {schema_path.name}")
    elif schema_identifier in identifiers:
        errors.append(f"duplicate schema $id: {schema_identifier}")
    else:
        identifiers.add(schema_identifier)
    if schema.get("type") != "object":
        errors.append(f"schema root must be an object: {schema_path.name}")
    if schema.get("additionalProperties") is not False:
        errors.append(f"schema root must reject additional properties: {schema_path.name}")
    return tuple(errors)


def _resolve_reference(root_schema: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    """Resolve a local JSON Pointer reference from *root_schema*."""
    if not reference.startswith("#/"):
        raise ValueError(f"only local JSON Pointer references are supported: {reference}")
    current: Any = root_schema
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        current = current[token]
    if not isinstance(current, Mapping):
        raise ValueError(f"reference does not resolve to a schema object: {reference}")
    return current


def _validate_node(
    root_schema: Mapping[str, Any],
    schema: SchemaNode,
    instance: Any,
    path: str,
) -> list[str]:
    """Validate one node and return stable path-qualified error messages."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: schema is false"]
    if "$ref" in schema:
        resolved = _resolve_reference(root_schema, str(schema["$ref"]))
        return _validate_node(root_schema, resolved, instance, path)

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(str(expected_type), instance):
        return [f"{path}: expected {expected_type}"]

    errors: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is not in the allowed enumeration")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not equal the required constant")

    if isinstance(instance, str):
        errors.extend(_validate_string(schema, instance, path))
    elif isinstance(instance, int) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            errors.append(f"{path}: integer is below the minimum")
    elif isinstance(instance, list):
        errors.extend(_validate_array(root_schema, schema, instance, path))
    elif isinstance(instance, dict):
        errors.extend(_validate_object(root_schema, schema, instance, path))

    if "oneOf" in schema:
        matching_branches = sum(
            not _validate_node(root_schema, branch, instance, path)
            for branch in schema["oneOf"]
        )
        if matching_branches != 1:
            errors.append(f"{path}: expected exactly one oneOf branch to match")
    return errors


def _matches_type(expected_type: str, instance: Any) -> bool:
    """Return whether *instance* matches a supported JSON Schema primitive."""
    type_checks = {
        "array": lambda value: isinstance(value, list),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "object": lambda value: isinstance(value, dict),
        "string": lambda value: isinstance(value, str),
    }
    checker = type_checks.get(expected_type)
    if checker is None:
        raise ValueError(f"unsupported schema type: {expected_type}")
    return checker(instance)


def _validate_string(schema: Mapping[str, Any], instance: str, path: str) -> list[str]:
    """Validate supported string constraints."""
    errors: list[str] = []
    minimum_length = schema.get("minLength")
    maximum_length = schema.get("maxLength")
    pattern = schema.get("pattern")
    if minimum_length is not None and len(instance) < minimum_length:
        errors.append(f"{path}: string is shorter than minLength")
    if maximum_length is not None and len(instance) > maximum_length:
        errors.append(f"{path}: string is longer than maxLength")
    if pattern is not None and re.search(str(pattern), instance) is None:
        errors.append(f"{path}: string does not match the required pattern")
    format_name = schema.get("format")
    if format_name is not None and not _matches_format(str(format_name), instance):
        errors.append(f"{path}: string does not match format {format_name}")
    return errors


def _matches_format(format_name: str, instance: str) -> bool:
    """Return whether *instance* satisfies a supported semantic string format."""
    try:
        if format_name == "uuid":
            UUID(instance)
        elif format_name == "date":
            date.fromisoformat(instance)
        elif format_name == "date-time":
            parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return False
        else:
            raise ValueError(f"unsupported string format: {format_name}")
    except (TypeError, ValueError):
        return False
    return True


def _validate_array(
    root_schema: Mapping[str, Any],
    schema: Mapping[str, Any],
    instance: list[Any],
    path: str,
) -> list[str]:
    """Validate supported array constraints and nested items."""
    errors: list[str] = []
    minimum_items = schema.get("minItems")
    maximum_items = schema.get("maxItems")
    if minimum_items is not None and len(instance) < minimum_items:
        errors.append(f"{path}: array has fewer than minItems")
    if maximum_items is not None and len(instance) > maximum_items:
        errors.append(f"{path}: array has more than maxItems")
    if schema.get("uniqueItems") and len({_canonical_value(item) for item in instance}) != len(instance):
        errors.append(f"{path}: array items must be unique")
    item_schema = schema.get("items")
    if isinstance(item_schema, (Mapping, bool)):
        for index, item in enumerate(instance):
            errors.extend(_validate_node(root_schema, item_schema, item, f"{path}[{index}]"))
    return errors


def _canonical_value(value: Any) -> str:
    """Produce a deterministic comparable representation for JSON values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_object(
    root_schema: Mapping[str, Any],
    schema: Mapping[str, Any],
    instance: dict[str, Any],
    path: str,
) -> list[str]:
    """Validate supported object constraints and nested properties."""
    errors: list[str] = []
    required = schema.get("required", [])
    for required_name in required:
        if required_name not in instance:
            errors.append(f"{path}: required property is missing: {required_name}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for property_name in sorted(set(instance) - set(properties)):
            errors.append(f"{path}: additional property is not allowed: {property_name}")
    for property_name, property_schema in properties.items():
        if property_name in instance:
            errors.extend(
                _validate_node(
                    root_schema,
                    property_schema,
                    instance[property_name],
                    f"{path}.{property_name}",
                )
            )
    return errors


if __name__ == "__main__":  # pragma: no cover - exercised through main().
    raise SystemExit(main())
