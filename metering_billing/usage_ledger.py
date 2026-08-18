"""In-memory third-normal-form ledger that mirrors the PostgreSQL core.

The ledger is the testable authority for this milestone.  A later PostgreSQL
adapter can implement the same registration and lookup methods without changing
ingestion rules.  Tables stay normalized: measurements reference events and
meter definitions; they do not copy tenant codes.

Generated identifiers use UUIDv7 when the interpreter provides it so local
Python 3.12 and CI Python 3.13 behave identically at the API boundary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import ModuleType
from typing import Callable
from uuid import UUID

from metering_billing.errors import RejectionReasonCode
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal

CREDIT_REASON_CODES = frozenset({"rating_correction", "goodwill", "billing_error"})
TAX_CODES = frozenset({"vat", "gst", "sales_tax"})


def generate_record_id(uuid_module: ModuleType = uuid) -> UUID:
    """Return a UUIDv7 when available, otherwise a random UUID4."""
    factory: Callable[[], UUID] = getattr(uuid_module, "uuid7", uuid_module.uuid4)
    return factory()


def _is_effective(valid_from: datetime, valid_to: datetime | None, occurred_at: datetime) -> bool:
    """Return whether *occurred_at* lies in ``[valid_from, valid_to)``."""
    if occurred_at < valid_from:
        return False
    if valid_to is None:
        return True
    return occurred_at < valid_to


@dataclass(frozen=True)
class TenantAccount:
    """Tenant authority boundary."""

    tenant_account_id: UUID
    tenant_reference: str
    tenant_account_code: str


@dataclass(frozen=True)
class BillingAccount:
    """Commercial payer scoped to one tenant."""

    billing_account_id: UUID
    tenant_account_id: UUID
    billing_account_reference: str
    billing_account_code: str
    account_status_code: str


@dataclass(frozen=True)
class BillingPrincipal:
    """Attribution subject scoped to one tenant and an effective interval."""

    billing_principal_id: UUID
    tenant_account_id: UUID
    billing_principal_reference: str
    principal_kind_code: str
    principal_reference: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class CredentialRecord:
    """Opaque credential fingerprint; never a plaintext secret."""

    credential_record_id: UUID
    tenant_account_id: UUID
    credential_reference: str
    credential_kind_code: str
    credential_fingerprint: str


@dataclass(frozen=True)
class CredentialAssignment:
    """Effective-dated link among credential, principal, and billing account."""

    credential_assignment_id: UUID
    tenant_account_id: UUID
    credential_record_id: UUID
    billing_principal_id: UUID
    billing_account_id: UUID
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class MeterDefinition:
    """Versioned meter unit and aggregation rule."""

    meter_definition_id: UUID
    meter_code: str
    meter_version: int
    unit_code: str
    aggregation_code: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class MeterQualityRule:
    """Billability disposition for one meter version and quality code."""

    meter_quality_rule_id: UUID
    meter_definition_id: UUID
    quality_code: str
    billing_disposition_code: str


@dataclass(frozen=True)
class StoredUsageMeasurement:
    """Normalized measurement row plus join projections for callers."""

    usage_measurement_id: UUID
    usage_event_id: UUID
    meter_definition_id: UUID
    meter_code: str
    unit_code: str
    measured_quantity: Decimal
    quality_code: str


@dataclass(frozen=True)
class StoredUsageEvent:
    """Immutable persisted usage fact."""

    usage_event_id: UUID
    producer_event_id: UUID
    tenant_account_id: UUID
    billing_account_id: UUID
    billing_principal_id: UUID
    credential_record_id: UUID | None
    source_event_key: str
    event_contract_version: int
    event_payload_hash: str
    product_code: str
    operation_code: str | None
    occurred_at: datetime
    recorded_at: datetime
    cost_center_reference: str | None
    project_reference: str | None
    measurements: tuple[StoredUsageMeasurement, ...]


@dataclass(frozen=True)
class StoredRateCard:
    """Tenant-scoped commercial price-book header.

    Versions live in ``rate_card_version``.  The header is identified by
    ``(tenant_account_id, rate_card_name)`` and is never a provider object ID.
    """

    rate_card_id: UUID
    tenant_account_id: UUID
    rate_card_name: str
    currency_code: str
    created_at: datetime


@dataclass(frozen=True)
class StoredRateCardLine:
    """Immutable flat unit price for one metric on one rate-card version."""

    rate_card_line_id: UUID
    tenant_account_id: UUID
    rate_card_version_id: UUID
    metric_code: str
    unit_amount: Decimal
    currency_code: str


@dataclass(frozen=True)
class StoredRateCardVersion:
    """Append-only published price list for one tenant rate card.

    Identity is tenant-scoped
    ``(rate_card_id, source_payload_hash, rate_card_contract_version)``.
    ``version_number`` increments on each distinct publish of the same card.
    """

    rate_card_version_id: UUID
    tenant_account_id: UUID
    rate_card_id: UUID
    version_number: int
    rate_card_contract_version: int
    currency_code: str
    source_payload_hash: str
    published_at: datetime
    rate_card_lines: tuple[StoredRateCardLine, ...]


@dataclass(frozen=True)
class StoredRatingLine:
    """Append-only invoice-intent line for one account and meter."""

    rating_line_id: UUID
    rating_run_id: UUID
    tenant_account_id: UUID
    billing_account_id: UUID
    billing_account_reference: str
    meter_definition_id: UUID
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal
    line_number: int


@dataclass(frozen=True)
class StoredRatingRun:
    """Append-only rating of one tenant window, rate card, and usage snapshot."""

    rating_run_id: UUID
    tenant_account_id: UUID
    rate_card_id: UUID
    rate_card_code: str
    rate_card_version: int
    window_started_at: datetime
    window_ended_at: datetime
    usage_snapshot_hash: str
    currency_code: str
    rated_total_amount: Decimal
    recorded_at: datetime
    rating_lines: tuple[StoredRatingLine, ...]


@dataclass(frozen=True)
class StoredTaxRateSchedule:
    """Tenant-scoped tax-rate header identified by ``(tenant, tax_code)``."""

    tax_rate_schedule_id: UUID
    tenant_account_id: UUID
    tax_code: str
    created_at: datetime


@dataclass(frozen=True)
class StoredTaxRateVersion:
    """Append-only published tax rate for one tenant schedule.

    Identity is tenant-scoped
    ``(tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)``.
    ``version_number`` increments on each distinct publish of the same code.
    """

    tax_rate_version_id: UUID
    tenant_account_id: UUID
    tax_rate_schedule_id: UUID
    version_number: int
    tax_rate_contract_version: int
    tax_code: str
    tax_rate: Decimal
    source_payload_hash: str
    published_at: datetime


@dataclass(frozen=True)
class StoredTaxAssessment:
    """Append-only commercial tax on one tenant invoice draft.

    Identity is
    ``(tenant_account_id, invoice_draft_id, tax_rate_version_id,
    source_payload_hash, tax_assessment_contract_version)``.
    One draft holds at most one assessment.
    """

    tax_assessment_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    tax_rate_version_id: UUID
    tax_assessment_contract_version: int
    tax_code: str
    tax_rate: Decimal
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    source_payload_hash: str
    assessed_at: datetime
    tax_rate_version_number: int


@dataclass(frozen=True)
class StoredInvoiceDraftLine:
    """Append-only invoice-intent draft line copied from one rating line."""

    invoice_draft_line_id: UUID
    invoice_draft_id: UUID
    tenant_account_id: UUID
    billing_account_id: UUID
    billing_account_reference: str
    meter_definition_id: UUID
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal
    line_number: int


@dataclass(frozen=True)
class StoredInvoiceDraft:
    """Append-only draft invoice intent for one tenant and rating run."""

    invoice_draft_id: UUID
    tenant_account_id: UUID
    rating_run_id: UUID
    usage_snapshot_hash: str
    currency_code: str
    invoice_draft_status: str
    drafted_total_amount: Decimal
    recorded_at: datetime
    invoice_draft_lines: tuple[StoredInvoiceDraftLine, ...]


@dataclass(frozen=True)
class StoredIssuedInvoiceLine:
    """Append-only commercial line frozen from one invoice-draft line."""

    issued_invoice_line_id: UUID
    issued_invoice_id: UUID
    tenant_account_id: UUID
    line_number: int
    billing_account_reference: str
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal


@dataclass(frozen=True)
class StoredIssuedInvoice:
    """Append-only commercial invoice snapshot issued from one tenant draft.

    Identity is ``(tenant_account_id, invoice_draft_id)``.  One draft holds
    at most one issued snapshot.  ``issued_invoice_id`` is the opaque
    generated invoice identifier, not a statutory number.
    """

    issued_invoice_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    issued_invoice_contract_version: int
    rating_run_id: UUID
    usage_snapshot_hash: str
    source_payload_hash: str
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    issued_invoice_status: str
    issued_at: datetime
    due_at: datetime | None
    issued_invoice_lines: tuple[StoredIssuedInvoiceLine, ...]


@dataclass(frozen=True)
class StoredIssuedCreditNote:
    """Append-only commercial credit-note snapshot issued from one credit.

    Identity is ``(tenant_account_id, credit_adjustment_id)``.  One credit
    holds at most one issued snapshot.  ``issued_credit_note_id`` is the
    opaque generated identifier, not a statutory number.
    """

    issued_credit_note_id: UUID
    tenant_account_id: UUID
    credit_adjustment_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    issued_credit_note_contract_version: int
    credit_adjustment_contract_version: int
    credit_reason_code: str
    credit_adjustment_source_payload_hash: str
    source_payload_hash: str
    currency_code: str
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    tax_inclusive_amount: Decimal
    issued_credit_note_status: str
    issued_at: datetime


@dataclass(frozen=True)
class StoredCreditNoteApplication:
    """Append-only application of one issued credit note onto one collection case.

    Identity is ``(tenant_account_id, issued_credit_note_id)``.  One issued
    credit note applies at most once.  ``credit_note_application_id`` is the
    opaque generated identifier.
    """

    credit_note_application_id: UUID
    tenant_account_id: UUID
    issued_credit_note_id: UUID
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    credit_note_application_contract_version: int
    issued_credit_note_contract_version: int
    source_payload_hash: str
    issued_credit_note_source_payload_hash: str
    currency_code: str
    applied_amount: Decimal
    credit_note_application_status: str
    applied_at: datetime


@dataclass(frozen=True)
class StoredCollectionCaseSettlement:
    """Append-only settle-when-zero fact for one collection case.

    Identity is ``(tenant_account_id, collection_case_id)``.  One case
    settles at most once through this command.  ``collection_case_settlement_id``
    is the opaque generated identifier.  This is not a payment receipt,
    write-off, or AIS posting.
    """

    collection_case_settlement_id: UUID
    tenant_account_id: UUID
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    collection_case_settlement_contract_version: int
    source_payload_hash: str
    currency_code: str
    remaining_outstanding_amount: Decimal
    collection_case_settlement_status: str
    settled_at: datetime


@dataclass(frozen=True)
class StoredCollectionWriteOff:
    """Append-only commercial write-off of leftover collection remaining.

    Identity is ``(tenant_account_id, collection_case_id)``.  One case
    writes off at most once through this command.  ``collection_write_off_id``
    is the opaque generated identifier.  This is not a payment receipt,
    credit note, settlement, journal, or AIS posting.
    """

    collection_write_off_id: UUID
    tenant_account_id: UUID
    collection_case_id: UUID
    invoice_draft_id: UUID
    issued_invoice_id: UUID | None
    collection_write_off_contract_version: int
    source_payload_hash: str
    currency_code: str
    write_off_amount: Decimal
    remaining_outstanding_amount: Decimal
    collection_write_off_status: str
    written_off_at: datetime


@dataclass(frozen=True)
class StoredJournalProposalLine:
    """Append-only proposal line using a semantic account role, not a chart ID."""

    journal_proposal_line_id: UUID
    journal_proposal_id: UUID
    tenant_account_id: UUID
    line_number: int
    account_role_code: str
    debit_amount: Decimal
    credit_amount: Decimal


@dataclass(frozen=True)
class StoredJournalProposal:
    """Append-only balanced journal proposal for one tenant draft, receipt, credit, or write-off."""

    journal_proposal_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    proposal_contract_version: int
    idempotency_key: str
    legal_entity_reference: str
    intended_book_role_code: str
    transaction_currency: str
    transaction_date: str
    accounting_date: str
    source_payload_hash: str
    proposed_at: datetime
    proposal_status: str
    source_event_reference: str
    proposal_lines: tuple[StoredJournalProposalLine, ...]
    payment_receipt_id: UUID | None = None
    credit_adjustment_id: UUID | None = None
    collection_write_off_id: UUID | None = None


@dataclass(frozen=True)
class StoredCollectionDunningEvent:
    """Append-only commercial reminder; it does not capture money or post books."""

    collection_dunning_event_id: UUID
    collection_case_id: UUID
    tenant_account_id: UUID
    dunning_event_number: int
    dunning_notice_code: str
    occurred_at: datetime


@dataclass(frozen=True)
class StoredCollectionCase:
    """Commercial collection case for one tenant invoice draft.

    Opening is append-only.  Applied receipts reduce ``outstanding_amount`` and
    may mark the current row ``settled``.
    """

    collection_case_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    currency_code: str
    collection_case_status: str
    outstanding_amount: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class StoredPaymentIntent:
    """Provider-neutral payment intent for one collection case.

    Projection is append-only.  Cancellation replaces the current status of the
    same identity row without writing a receipt.
    """

    payment_intent_id: UUID
    tenant_account_id: UUID
    collection_case_id: UUID
    payment_intent_contract_version: int
    currency_code: str
    payment_intent_status: str
    payment_amount: Decimal
    source_payload_hash: str
    projected_at: datetime


@dataclass(frozen=True)
class StoredUnappliedCash:
    """Append-only leftover remittance parked against one payment receipt.

    Identity is ``(tenant_account_id, payment_receipt_id)``.  One receipt
    parks leftover at most once.  ``unapplied_cash_id`` is the opaque
    generated identifier.  This is not a journal, webhook, write-off,
    settlement, credit note, or AIS posting.
    """

    unapplied_cash_id: UUID
    tenant_account_id: UUID
    payment_receipt_id: UUID
    payment_intent_id: UUID
    collection_case_id: UUID
    unapplied_cash_contract_version: int
    source_payload_hash: str
    currency_code: str
    unapplied_amount: Decimal
    received_amount: Decimal
    applied_amount: Decimal
    unapplied_cash_status: str
    parked_at: datetime


@dataclass(frozen=True)
class StoredUnappliedCashApplication:
    """Append-only apply of one parked leftover onto one collection case.

    Identity is ``(tenant_account_id, unapplied_cash_id)``.  One parked
    leftover applies at most once.  ``unapplied_cash_application_id`` is
    the opaque generated identifier.  This is not a journal, webhook,
    write-off, settlement, credit note, or AIS posting.
    """

    unapplied_cash_application_id: UUID
    tenant_account_id: UUID
    unapplied_cash_id: UUID
    collection_case_id: UUID
    payment_receipt_id: UUID
    invoice_draft_id: UUID
    unapplied_cash_application_contract_version: int
    source_payload_hash: str
    currency_code: str
    applied_amount: Decimal
    unapplied_cash_application_status: str
    applied_at: datetime


@dataclass(frozen=True)
class StoredUnappliedCashRefund:
    """Append-only commercial refund of one parked leftover.

    Identity is ``(tenant_account_id, unapplied_cash_id)``.  One parked
    leftover refunds at most once.  ``unapplied_cash_refund_id`` is the
    opaque generated identifier.  This is not a journal, webhook,
    write-off, settlement, credit note, PSP capture, or AIS posting.
    """

    unapplied_cash_refund_id: UUID
    tenant_account_id: UUID
    unapplied_cash_id: UUID
    payment_receipt_id: UUID
    payment_intent_id: UUID
    collection_case_id: UUID
    unapplied_cash_refund_contract_version: int
    source_payload_hash: str
    currency_code: str
    refund_amount: Decimal
    unapplied_amount: Decimal
    unapplied_cash_refund_status: str
    refunded_at: datetime


@dataclass(frozen=True)
class StoredPaymentReceipt:
    """Append-only commercial receipt applied against one projected intent."""

    payment_receipt_id: UUID
    tenant_account_id: UUID
    payment_intent_id: UUID
    collection_case_id: UUID
    settlement_contract_version: int
    currency_code: str
    payment_receipt_status: str
    received_amount: Decimal
    source_payload_hash: str
    received_at: datetime


@dataclass(frozen=True)
class StoredCreditAdjustment:
    """Persisted commercial credit against one invoice draft.

    Identity is tenant-scoped
    ``(invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)``.
    The internal primary key is ``credit_adjustment_id``, never a provider
    object identifier.
    """

    credit_adjustment_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    credit_adjustment_contract_version: int
    credit_reason_code: str
    currency_code: str
    credit_amount: Decimal
    tax_exclusive_amount: Decimal
    tax_amount: Decimal
    source_payload_hash: str
    recorded_at: datetime


@dataclass(frozen=True)
class StoredPostingReceiptObservation:
    """Append-only commercial observation of one AIS posting receipt.

    AIS ``receipt_id`` is stored as an external reference.  It is never the
    internal primary key.  ``posting_status_code`` is AIS-owned and is not
    mapped onto journal ``proposal_status``.
    """

    posting_receipt_observation_id: UUID
    tenant_account_id: UUID
    receipt_id: UUID
    receipt_contract_version: int
    idempotency_key: str
    source_proposal_id: UUID
    source_payload_hash: str
    legal_entity_reference: str
    accounting_book_reference: str
    accounting_policy_version: str
    posting_rule_version: str
    posting_status_code: str
    recorded_at: str
    fiscal_period_reference: str | None
    journal_reference: str | None
    reversal_of_journal_reference: str | None
    hold_reason_code: str | None
    rejection_reason_code: str | None
    posted_at: str | None
    line_count: int | None
    transaction_currency: str | None
    functional_currency: str | None
    observed_at: str


@dataclass(frozen=True)
class StoredIngestionReceipt:
    """Append-only audit row for one ingest attempt."""

    usage_ingestion_receipt_id: UUID
    tenant_account_id: UUID | None
    usage_event_id: UUID | None
    source_event_key: str
    event_contract_version: int | None
    source_payload_hash: str | None
    ingestion_outcome_code: str
    rejection_reason_code: str | None
    recorded_at: datetime


@dataclass(frozen=True)
class StoredTenantApiCredential:
    """Append-only HTTP API credential for one tenant.

    The plaintext secret is never stored.  ``credential_secret_hash`` is a
    keyed HMAC.  Revocation updates ``credential_status`` on the same row.
    """

    tenant_api_credential_id: UUID
    tenant_account_id: UUID
    tenant_api_credential_contract_version: int
    credential_label: str
    credential_prefix: str
    credential_secret_hash: str
    credential_status: str
    issued_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class StoredWebhookSubscription:
    """Tenant-scoped webhook callback.  SQL stores hash and prefix only."""

    webhook_subscription_id: UUID
    tenant_account_id: UUID
    webhook_subscription_contract_version: int
    callback_url: str
    event_type_set: str
    webhook_secret_prefix: str
    webhook_secret_hash: str
    subscription_status: str
    issued_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class StoredWebhookOutboxEvent:
    """Append-only commercial fact waiting for webhook delivery."""

    outbox_event_id: UUID
    tenant_account_id: UUID
    event_type_code: str
    payload_hash: str
    source_id: UUID
    occurred_at: datetime
    delivery_status: str
    payload_json: str
    enqueued_at: datetime


@dataclass(frozen=True)
class StoredWebhookDeliveryAttempt:
    """Append-only POST attempt against one subscription and outbox event."""

    delivery_attempt_id: UUID
    outbox_event_id: UUID
    webhook_subscription_id: UUID
    attempt_number: int
    http_status: int | None
    delivered_at: datetime | None
    failure_reason_code: str | None
    attempted_at: datetime


@dataclass
class MemoryUsageLedger:
    """Mutable catalog plus append-only usage tables with tenant isolation."""

    tenant_accounts: dict[str, TenantAccount] = field(default_factory=dict)
    billing_accounts: dict[str, BillingAccount] = field(default_factory=dict)
    billing_principals: dict[str, BillingPrincipal] = field(default_factory=dict)
    credential_records: dict[str, CredentialRecord] = field(default_factory=dict)
    credential_assignments: list[CredentialAssignment] = field(default_factory=list)
    meter_definitions: list[MeterDefinition] = field(default_factory=list)
    meter_quality_rules: dict[tuple[UUID, str], MeterQualityRule] = field(default_factory=dict)
    usage_events: dict[UUID, StoredUsageEvent] = field(default_factory=dict)
    source_event_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    payload_hash_index: dict[tuple[UUID, str, int], UUID] = field(default_factory=dict)
    producer_event_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    usage_ingestion_receipts: list[StoredIngestionReceipt] = field(default_factory=list)
    accounting_export_records: list[dict[str, str]] = field(default_factory=list)
    rate_cards: dict[UUID, StoredRateCard] = field(default_factory=dict)
    rate_card_name_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    rate_card_versions: dict[UUID, StoredRateCardVersion] = field(default_factory=dict)
    rate_card_version_index: dict[tuple[UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    rate_card_version_number_index: dict[tuple[UUID, UUID, int], UUID] = field(
        default_factory=dict
    )
    rate_card_lines: list[StoredRateCardLine] = field(default_factory=list)
    rating_runs: dict[UUID, StoredRatingRun] = field(default_factory=dict)
    rating_run_index: dict[tuple[UUID, datetime, datetime, UUID, str], UUID] = field(
        default_factory=dict
    )
    rating_lines: list[StoredRatingLine] = field(default_factory=list)
    invoice_drafts: dict[UUID, StoredInvoiceDraft] = field(default_factory=dict)
    invoice_draft_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    invoice_draft_lines: list[StoredInvoiceDraftLine] = field(default_factory=list)
    journal_proposals: dict[UUID, StoredJournalProposal] = field(default_factory=dict)
    journal_proposal_index: dict[tuple[UUID, UUID, str, int], UUID] = field(default_factory=dict)
    cash_journal_proposal_index: dict[tuple[UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    credit_journal_proposal_index: dict[tuple[UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    credit_journal_adjustment_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    write_off_journal_proposal_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    credit_adjustments: dict[UUID, StoredCreditAdjustment] = field(default_factory=dict)
    credit_adjustment_index: dict[tuple[UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    journal_proposal_lines: list[StoredJournalProposalLine] = field(default_factory=list)
    collection_cases: dict[UUID, StoredCollectionCase] = field(default_factory=dict)
    collection_case_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    collection_dunning_events: list[StoredCollectionDunningEvent] = field(default_factory=list)
    collection_dunning_notice_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    collection_dunning_number_index: dict[tuple[UUID, int], UUID] = field(default_factory=dict)
    payment_intents: dict[UUID, StoredPaymentIntent] = field(default_factory=dict)
    payment_intent_index: dict[tuple[UUID, UUID, str, int], UUID] = field(default_factory=dict)
    payment_receipts: dict[UUID, StoredPaymentReceipt] = field(default_factory=dict)
    payment_receipt_index: dict[tuple[UUID, UUID, str, int], UUID] = field(default_factory=dict)
    posting_receipt_observations: dict[UUID, StoredPostingReceiptObservation] = field(
        default_factory=dict
    )
    posting_receipt_observation_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    posting_receipt_observation_receipt_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    tax_rate_schedules: dict[UUID, StoredTaxRateSchedule] = field(default_factory=dict)
    tax_rate_schedule_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    tax_rate_versions: dict[UUID, StoredTaxRateVersion] = field(default_factory=dict)
    tax_rate_version_index: dict[tuple[UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    tax_rate_version_number_index: dict[tuple[UUID, UUID, int], UUID] = field(
        default_factory=dict
    )
    tax_assessments: dict[UUID, StoredTaxAssessment] = field(default_factory=dict)
    tax_assessment_index: dict[tuple[UUID, UUID, UUID, str, int], UUID] = field(
        default_factory=dict
    )
    tax_assessment_draft_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    tenant_api_credentials: dict[UUID, StoredTenantApiCredential] = field(default_factory=dict)
    tenant_api_credential_hash_index: dict[str, UUID] = field(default_factory=dict)
    webhook_subscriptions: dict[UUID, StoredWebhookSubscription] = field(default_factory=dict)
    webhook_subscription_identity_index: dict[tuple[UUID, str, str, int], UUID] = field(
        default_factory=dict
    )
    webhook_subscription_hash_index: dict[str, UUID] = field(default_factory=dict)
    webhook_subscription_secrets: dict[UUID, str] = field(default_factory=dict)
    webhook_outbox_events: dict[UUID, StoredWebhookOutboxEvent] = field(default_factory=dict)
    webhook_outbox_identity_index: dict[tuple[UUID, str, UUID, str], UUID] = field(
        default_factory=dict
    )
    webhook_delivery_attempts: list[StoredWebhookDeliveryAttempt] = field(default_factory=list)
    issued_invoices: dict[UUID, StoredIssuedInvoice] = field(default_factory=dict)
    issued_invoice_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    issued_invoice_lines: list[StoredIssuedInvoiceLine] = field(default_factory=list)
    issued_credit_notes: dict[UUID, StoredIssuedCreditNote] = field(default_factory=dict)
    issued_credit_note_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    credit_note_applications: dict[UUID, StoredCreditNoteApplication] = field(
        default_factory=dict
    )
    credit_note_application_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    collection_case_settlements: dict[UUID, StoredCollectionCaseSettlement] = field(
        default_factory=dict
    )
    collection_case_settlement_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    collection_write_offs: dict[UUID, StoredCollectionWriteOff] = field(default_factory=dict)
    collection_write_off_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    unapplied_cash: dict[UUID, StoredUnappliedCash] = field(default_factory=dict)
    unapplied_cash_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    unapplied_cash_applications: dict[UUID, StoredUnappliedCashApplication] = field(
        default_factory=dict
    )
    unapplied_cash_application_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )
    unapplied_cash_refunds: dict[UUID, StoredUnappliedCashRefund] = field(
        default_factory=dict
    )
    unapplied_cash_refund_index: dict[tuple[UUID, UUID], UUID] = field(
        default_factory=dict
    )

    def register_tenant(self, tenant_reference: str) -> TenantAccount:
        """Register a tenant authority.  Re-registering the same URN is idempotent."""
        existing = self.tenant_accounts.get(tenant_reference)
        if existing is not None:
            return existing
        tenant = TenantAccount(
            tenant_account_id=generate_record_id(),
            tenant_reference=tenant_reference,
            tenant_account_code=_single_urn_segment(tenant_reference),
        )
        self.tenant_accounts[tenant_reference] = tenant
        return tenant

    def register_billing_account(
        self,
        tenant_reference: str,
        billing_account_reference: str,
        account_status_code: str = "active",
    ) -> BillingAccount:
        """Register a tenant-scoped billing account."""
        tenant = self.require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, billing_account_reference)
        existing = self.billing_accounts.get(billing_account_reference)
        if existing is not None:
            if existing.tenant_account_id != tenant.tenant_account_id:
                raise ValueError("billing account cannot move across tenants")
            return existing
        account = BillingAccount(
            billing_account_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            billing_account_reference=billing_account_reference,
            billing_account_code=_resource_code(billing_account_reference),
            account_status_code=account_status_code,
        )
        self.billing_accounts[billing_account_reference] = account
        return account

    def register_billing_principal(
        self,
        tenant_reference: str,
        billing_principal_reference: str,
        principal_kind_code: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> BillingPrincipal:
        """Register a tenant-scoped principal with an effective interval."""
        tenant = self.require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, billing_principal_reference)
        existing = self.billing_principals.get(billing_principal_reference)
        if existing is not None:
            if existing.tenant_account_id != tenant.tenant_account_id:
                raise ValueError("billing principal cannot move across tenants")
            return existing
        principal = BillingPrincipal(
            billing_principal_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            billing_principal_reference=billing_principal_reference,
            principal_kind_code=principal_kind_code,
            principal_reference=billing_principal_reference,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.billing_principals[billing_principal_reference] = principal
        return principal

    def register_credential_record(
        self,
        tenant_reference: str,
        credential_reference: str,
        credential_kind_code: str,
        credential_fingerprint: str,
    ) -> CredentialRecord:
        """Register an opaque credential fingerprint for one tenant."""
        tenant = self.require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, credential_reference)
        existing = self.credential_records.get(credential_reference)
        if existing is not None:
            if existing.tenant_account_id != tenant.tenant_account_id:
                raise ValueError("credential record cannot move across tenants")
            return existing
        record = CredentialRecord(
            credential_record_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            credential_reference=credential_reference,
            credential_kind_code=credential_kind_code,
            credential_fingerprint=credential_fingerprint,
        )
        self.credential_records[credential_reference] = record
        return record

    def register_credential_assignment(
        self,
        tenant_reference: str,
        credential_reference: str,
        billing_principal_reference: str,
        billing_account_reference: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> CredentialAssignment:
        """Bind a credential to a principal and billing account inside one tenant."""
        tenant = self.require_tenant(tenant_reference)
        credential = self.credential_records[credential_reference]
        principal = self.billing_principals[billing_principal_reference]
        account = self.billing_accounts[billing_account_reference]
        if {
            credential.tenant_account_id,
            principal.tenant_account_id,
            account.tenant_account_id,
        } != {tenant.tenant_account_id}:
            raise ValueError("credential assignment cannot cross tenants")
        assignment = CredentialAssignment(
            credential_assignment_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            credential_record_id=credential.credential_record_id,
            billing_principal_id=principal.billing_principal_id,
            billing_account_id=account.billing_account_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.credential_assignments.append(assignment)
        return assignment

    def register_meter_definition(
        self,
        meter_code: str,
        meter_version: int,
        unit_code: str,
        aggregation_code: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> MeterDefinition:
        """Register a versioned meter.  The same code and version is idempotent."""
        for existing in self.meter_definitions:
            if existing.meter_code == meter_code and existing.meter_version == meter_version:
                return existing
        definition = MeterDefinition(
            meter_definition_id=generate_record_id(),
            meter_code=meter_code,
            meter_version=meter_version,
            unit_code=unit_code,
            aggregation_code=aggregation_code,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.meter_definitions.append(definition)
        return definition

    def register_meter_quality_rule(
        self,
        meter_definition_id: UUID,
        quality_code: str,
        billing_disposition_code: str,
    ) -> MeterQualityRule:
        """Register the disposition for one meter version and quality code."""
        key = (meter_definition_id, quality_code)
        existing = self.meter_quality_rules.get(key)
        if existing is not None:
            return existing
        rule = MeterQualityRule(
            meter_quality_rule_id=generate_record_id(),
            meter_definition_id=meter_definition_id,
            quality_code=quality_code,
            billing_disposition_code=billing_disposition_code,
        )
        self.meter_quality_rules[key] = rule
        return rule

    def find_rate_card(self, tenant_account_id: UUID, rate_card_name: str) -> StoredRateCard | None:
        """Return the tenant-scoped rate-card header for one name, if present."""
        rate_card_id = self.rate_card_name_index.get((tenant_account_id, rate_card_name))
        if rate_card_id is None:
            return None
        return self.rate_cards[rate_card_id]

    def get_rate_card(self, rate_card_id: UUID) -> StoredRateCard | None:
        """Return one rate-card header by internal identifier, if present."""
        return self.rate_cards.get(rate_card_id)

    def insert_rate_card(self, rate_card: StoredRateCard) -> StoredRateCard:
        """Persist one tenant-scoped rate-card header or return the existing name."""
        existing_id = self.rate_card_name_index.get(
            (rate_card.tenant_account_id, rate_card.rate_card_name)
        )
        if existing_id is not None:
            existing = self.rate_cards[existing_id]
            if existing.currency_code != rate_card.currency_code:
                raise ValueError("rate_card currency cannot change after publish")
            return existing
        if rate_card.rate_card_id in self.rate_cards:
            raise ValueError("rate_card_id already stored for a different name")
        self.rate_cards[rate_card.rate_card_id] = rate_card
        self.rate_card_name_index[(rate_card.tenant_account_id, rate_card.rate_card_name)] = (
            rate_card.rate_card_id
        )
        return rate_card

    def list_rate_cards(self, tenant_account_id: UUID) -> tuple[StoredRateCard, ...]:
        """Return rate-card headers limited to one tenant."""
        return tuple(
            card
            for card in self.rate_cards.values()
            if card.tenant_account_id == tenant_account_id
        )

    def find_rate_card_version_by_identity(
        self,
        tenant_account_id: UUID,
        rate_card_id: UUID,
        source_payload_hash: str,
        rate_card_contract_version: int,
    ) -> StoredRateCardVersion | None:
        """Return the published version for one tenant-scoped line hash, if any."""
        version_id = self.rate_card_version_index.get(
            (
                tenant_account_id,
                rate_card_id,
                source_payload_hash,
                rate_card_contract_version,
            )
        )
        if version_id is None:
            return None
        return self.rate_card_versions[version_id]

    def get_rate_card_version(
        self, rate_card_version_id: UUID
    ) -> StoredRateCardVersion | None:
        """Return one published version by internal identifier, if present."""
        return self.rate_card_versions.get(rate_card_version_id)

    def find_rate_card_version(
        self,
        tenant_account_id: UUID,
        version_number: int,
        rate_card_name: str | None = None,
    ) -> StoredRateCardVersion | None:
        """Return one tenant-scoped published version number, if uniquely present."""
        if rate_card_name is not None:
            card = self.find_rate_card(tenant_account_id, rate_card_name)
            if card is None:
                return None
            version_id = self.rate_card_version_number_index.get(
                (tenant_account_id, card.rate_card_id, version_number)
            )
            if version_id is None:
                return None
            return self.rate_card_versions[version_id]
        matches = tuple(
            version
            for version in self.rate_card_versions.values()
            if version.tenant_account_id == tenant_account_id
            and version.version_number == version_number
        )
        if len(matches) != 1:
            return None
        return matches[0]

    def next_rate_card_version_number(
        self, tenant_account_id: UUID, rate_card_id: UUID
    ) -> int:
        """Return the next append-only version number for one tenant card."""
        current = [
            version.version_number
            for version in self.rate_card_versions.values()
            if version.tenant_account_id == tenant_account_id
            and version.rate_card_id == rate_card_id
        ]
        if not current:
            return 1
        return max(current) + 1

    def insert_rate_card_version(
        self, version: StoredRateCardVersion
    ) -> StoredRateCardVersion:
        """Persist one immutable published version or return the existing identity."""
        if version.version_number < 1:
            raise ValueError("version_number must be greater than zero")
        metric_codes = [line.metric_code for line in version.rate_card_lines]
        if not metric_codes or len(set(metric_codes)) != len(metric_codes):
            raise ValueError("rate_card_lines must be a unique non-empty metric set")
        for line in version.rate_card_lines:
            if line.unit_amount <= 0:
                raise ValueError("unit_amount must be greater than zero")
            if line.currency_code != version.currency_code:
                raise ValueError("rate_card_line currency must match the version")
        identity = (
            version.tenant_account_id,
            version.rate_card_id,
            version.source_payload_hash,
            version.rate_card_contract_version,
        )
        existing_id = self.rate_card_version_index.get(identity)
        if existing_id is not None:
            return self.rate_card_versions[existing_id]
        if version.rate_card_version_id in self.rate_card_versions:
            raise ValueError("rate_card_version_id already stored for a different identity")
        persisted_lines = tuple(
            StoredRateCardLine(
                rate_card_line_id=line.rate_card_line_id,
                tenant_account_id=line.tenant_account_id,
                rate_card_version_id=version.rate_card_version_id,
                metric_code=line.metric_code,
                unit_amount=parse_exact_decimal(format_exact_decimal(line.unit_amount)),
                currency_code=line.currency_code,
            )
            for line in version.rate_card_lines
        )
        persisted = StoredRateCardVersion(
            rate_card_version_id=version.rate_card_version_id,
            tenant_account_id=version.tenant_account_id,
            rate_card_id=version.rate_card_id,
            version_number=version.version_number,
            rate_card_contract_version=version.rate_card_contract_version,
            currency_code=version.currency_code,
            source_payload_hash=version.source_payload_hash,
            published_at=version.published_at,
            rate_card_lines=persisted_lines,
        )
        self.rate_card_versions[persisted.rate_card_version_id] = persisted
        self.rate_card_version_index[identity] = persisted.rate_card_version_id
        self.rate_card_version_number_index[
            (persisted.tenant_account_id, persisted.rate_card_id, persisted.version_number)
        ] = persisted.rate_card_version_id
        self.rate_card_lines.extend(persisted_lines)
        return persisted

    def list_rate_card_versions(
        self, tenant_account_id: UUID, rate_card_id: UUID | None = None
    ) -> tuple[StoredRateCardVersion, ...]:
        """Return published versions, optionally limited to one tenant card."""
        versions = tuple(
            version
            for version in self.rate_card_versions.values()
            if version.tenant_account_id == tenant_account_id
        )
        if rate_card_id is None:
            return versions
        return tuple(version for version in versions if version.rate_card_id == rate_card_id)

    def find_rate_card_line(
        self, rate_card_version_id: UUID, metric_code: str
    ) -> StoredRateCardLine | None:
        """Return the stored unit amount for one metric on one published version."""
        version = self.rate_card_versions.get(rate_card_version_id)
        if version is None:
            return None
        for line in version.rate_card_lines:
            if line.metric_code == metric_code:
                return line
        return None

    def find_tax_rate_schedule(
        self, tenant_account_id: UUID, tax_code: str
    ) -> StoredTaxRateSchedule | None:
        """Return the tenant-scoped tax-rate schedule for one code, if present."""
        schedule_id = self.tax_rate_schedule_index.get((tenant_account_id, tax_code))
        if schedule_id is None:
            return None
        return self.tax_rate_schedules[schedule_id]

    def get_tax_rate_schedule(
        self, tax_rate_schedule_id: UUID
    ) -> StoredTaxRateSchedule | None:
        """Return one tax-rate schedule by internal identifier, if present."""
        return self.tax_rate_schedules.get(tax_rate_schedule_id)

    def insert_tax_rate_schedule(
        self, schedule: StoredTaxRateSchedule
    ) -> StoredTaxRateSchedule:
        """Persist one tenant-scoped tax-rate schedule or return the existing code."""
        if schedule.tax_code not in TAX_CODES:
            raise ValueError("tax_code is not in the closed set")
        existing_id = self.tax_rate_schedule_index.get(
            (schedule.tenant_account_id, schedule.tax_code)
        )
        if existing_id is not None:
            return self.tax_rate_schedules[existing_id]
        if schedule.tax_rate_schedule_id in self.tax_rate_schedules:
            raise ValueError("tax_rate_schedule_id already stored for a different code")
        self.tax_rate_schedules[schedule.tax_rate_schedule_id] = schedule
        self.tax_rate_schedule_index[(schedule.tenant_account_id, schedule.tax_code)] = (
            schedule.tax_rate_schedule_id
        )
        return schedule

    def list_tax_rate_schedules(
        self, tenant_account_id: UUID
    ) -> tuple[StoredTaxRateSchedule, ...]:
        """Return tax-rate schedules limited to one tenant."""
        return tuple(
            schedule
            for schedule in self.tax_rate_schedules.values()
            if schedule.tenant_account_id == tenant_account_id
        )

    def find_tax_rate_version_by_identity(
        self,
        tenant_account_id: UUID,
        tax_rate_schedule_id: UUID,
        source_payload_hash: str,
        tax_rate_contract_version: int,
    ) -> StoredTaxRateVersion | None:
        """Return the published version for one tenant-scoped rate hash, if any."""
        version_id = self.tax_rate_version_index.get(
            (
                tenant_account_id,
                tax_rate_schedule_id,
                source_payload_hash,
                tax_rate_contract_version,
            )
        )
        if version_id is None:
            return None
        return self.tax_rate_versions[version_id]

    def get_tax_rate_version(
        self, tax_rate_version_id: UUID
    ) -> StoredTaxRateVersion | None:
        """Return one published tax-rate version by internal identifier, if present."""
        return self.tax_rate_versions.get(tax_rate_version_id)

    def find_tax_rate_version(
        self,
        tenant_account_id: UUID,
        version_number: int,
        tax_code: str | None = None,
    ) -> StoredTaxRateVersion | None:
        """Return one tenant-scoped published version number, if uniquely present."""
        if tax_code is not None:
            schedule = self.find_tax_rate_schedule(tenant_account_id, tax_code)
            if schedule is None:
                return None
            version_id = self.tax_rate_version_number_index.get(
                (tenant_account_id, schedule.tax_rate_schedule_id, version_number)
            )
            if version_id is None:
                return None
            return self.tax_rate_versions[version_id]
        matches = tuple(
            version
            for version in self.tax_rate_versions.values()
            if version.tenant_account_id == tenant_account_id
            and version.version_number == version_number
        )
        if len(matches) != 1:
            return None
        return matches[0]

    def next_tax_rate_version_number(
        self, tenant_account_id: UUID, tax_rate_schedule_id: UUID
    ) -> int:
        """Return the next append-only version number for one tenant schedule."""
        current = [
            version.version_number
            for version in self.tax_rate_versions.values()
            if version.tenant_account_id == tenant_account_id
            and version.tax_rate_schedule_id == tax_rate_schedule_id
        ]
        if not current:
            return 1
        return max(current) + 1

    def insert_tax_rate_version(
        self, version: StoredTaxRateVersion
    ) -> StoredTaxRateVersion:
        """Persist one immutable published tax rate or return the existing identity."""
        if version.version_number < 1:
            raise ValueError("version_number must be greater than zero")
        if version.tax_code not in TAX_CODES:
            raise ValueError("tax_code is not in the closed set")
        parsed_rate = parse_exact_decimal(format_exact_decimal(version.tax_rate))
        if parsed_rate > 1:
            raise ValueError("tax_rate must be an exact decimal in [0, 1]")
        identity = (
            version.tenant_account_id,
            version.tax_rate_schedule_id,
            version.source_payload_hash,
            version.tax_rate_contract_version,
        )
        existing_id = self.tax_rate_version_index.get(identity)
        if existing_id is not None:
            return self.tax_rate_versions[existing_id]
        if version.tax_rate_version_id in self.tax_rate_versions:
            raise ValueError("tax_rate_version_id already stored for a different identity")
        persisted = StoredTaxRateVersion(
            tax_rate_version_id=version.tax_rate_version_id,
            tenant_account_id=version.tenant_account_id,
            tax_rate_schedule_id=version.tax_rate_schedule_id,
            version_number=version.version_number,
            tax_rate_contract_version=version.tax_rate_contract_version,
            tax_code=version.tax_code,
            tax_rate=parsed_rate,
            source_payload_hash=version.source_payload_hash,
            published_at=version.published_at,
        )
        self.tax_rate_versions[persisted.tax_rate_version_id] = persisted
        self.tax_rate_version_index[identity] = persisted.tax_rate_version_id
        self.tax_rate_version_number_index[
            (persisted.tenant_account_id, persisted.tax_rate_schedule_id, persisted.version_number)
        ] = persisted.tax_rate_version_id
        return persisted

    def list_tax_rate_versions(
        self, tenant_account_id: UUID, tax_rate_schedule_id: UUID | None = None
    ) -> tuple[StoredTaxRateVersion, ...]:
        """Return published tax-rate versions, optionally limited to one schedule."""
        versions = tuple(
            version
            for version in self.tax_rate_versions.values()
            if version.tenant_account_id == tenant_account_id
        )
        if tax_rate_schedule_id is None:
            return versions
        return tuple(
            version for version in versions if version.tax_rate_schedule_id == tax_rate_schedule_id
        )

    def find_tax_assessment(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        tax_rate_version_id: UUID,
        source_payload_hash: str,
        tax_assessment_contract_version: int,
    ) -> StoredTaxAssessment | None:
        """Return the assessment for one tenant-scoped identity, if any."""
        assessment_id = self.tax_assessment_index.get(
            (
                tenant_account_id,
                invoice_draft_id,
                tax_rate_version_id,
                source_payload_hash,
                tax_assessment_contract_version,
            )
        )
        if assessment_id is None:
            return None
        return self.tax_assessments[assessment_id]

    def find_tax_assessment_for_draft(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredTaxAssessment | None:
        """Return the single assessment stored for one tenant draft, if any."""
        assessment_id = self.tax_assessment_draft_index.get(
            (tenant_account_id, invoice_draft_id)
        )
        if assessment_id is None:
            return None
        return self.tax_assessments[assessment_id]

    def get_tax_assessment(
        self, tax_assessment_id: UUID
    ) -> StoredTaxAssessment | None:
        """Return one tax assessment by internal identifier, if present."""
        return self.tax_assessments.get(tax_assessment_id)

    def list_tax_assessments(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredTaxAssessment, ...]:
        """Return stored tax assessments, optionally filtered by tenant."""
        assessments = tuple(self.tax_assessments.values())
        if tenant_account_id is None:
            return assessments
        return tuple(
            assessment
            for assessment in assessments
            if assessment.tenant_account_id == tenant_account_id
        )

    def insert_tax_assessment(
        self, assessment: StoredTaxAssessment
    ) -> StoredTaxAssessment:
        """Persist one tax assessment or return the existing identity row."""
        if assessment.tax_code not in TAX_CODES:
            raise ValueError("tax_code is not in the closed set")
        exclusive = parse_exact_decimal(format_exact_decimal(assessment.tax_exclusive_amount))
        tax_amount = parse_exact_decimal(format_exact_decimal(assessment.tax_amount))
        inclusive = parse_exact_decimal(format_exact_decimal(assessment.tax_inclusive_amount))
        if exclusive <= 0:
            raise ValueError("tax_exclusive_amount must be greater than zero")
        if inclusive != exclusive + tax_amount:
            raise ValueError("tax_inclusive_amount must equal exclusive plus tax")
        parsed_rate = parse_exact_decimal(format_exact_decimal(assessment.tax_rate))
        if parsed_rate > 1:
            raise ValueError("tax_rate must be an exact decimal in [0, 1]")
        identity = (
            assessment.tenant_account_id,
            assessment.invoice_draft_id,
            assessment.tax_rate_version_id,
            assessment.source_payload_hash,
            assessment.tax_assessment_contract_version,
        )
        existing_id = self.tax_assessment_index.get(identity)
        if existing_id is not None:
            return self.tax_assessments[existing_id]
        draft_key = (assessment.tenant_account_id, assessment.invoice_draft_id)
        existing_draft_id = self.tax_assessment_draft_index.get(draft_key)
        if existing_draft_id is not None:
            raise ValueError("invoice draft already has a tax assessment")
        if assessment.tax_assessment_id in self.tax_assessments:
            raise ValueError("tax_assessment_id already stored for a different identity")
        persisted = StoredTaxAssessment(
            tax_assessment_id=assessment.tax_assessment_id,
            tenant_account_id=assessment.tenant_account_id,
            invoice_draft_id=assessment.invoice_draft_id,
            tax_rate_version_id=assessment.tax_rate_version_id,
            tax_assessment_contract_version=assessment.tax_assessment_contract_version,
            tax_code=assessment.tax_code,
            tax_rate=parsed_rate,
            currency_code=assessment.currency_code,
            tax_exclusive_amount=exclusive,
            tax_amount=tax_amount,
            tax_inclusive_amount=inclusive,
            source_payload_hash=assessment.source_payload_hash,
            assessed_at=assessment.assessed_at,
            tax_rate_version_number=assessment.tax_rate_version_number,
        )
        self.tax_assessments[persisted.tax_assessment_id] = persisted
        self.tax_assessment_index[identity] = persisted.tax_assessment_id
        self.tax_assessment_draft_index[draft_key] = persisted.tax_assessment_id
        return persisted

    def billing_account_reference_for(self, billing_account_id: UUID) -> str:
        """Return the catalog URN for a stored billing account identifier."""
        for account in self.billing_accounts.values():
            if account.billing_account_id == billing_account_id:
                return account.billing_account_reference
        raise KeyError(billing_account_id)

    def find_rating_run(
        self,
        tenant_account_id: UUID,
        window_started_at: datetime,
        window_ended_at: datetime,
        rate_card_id: UUID,
        usage_snapshot_hash: str,
    ) -> StoredRatingRun | None:
        """Return the append-only run for one rating identity, if it exists."""
        rating_run_id = self.rating_run_index.get(
            (
                tenant_account_id,
                window_started_at,
                window_ended_at,
                rate_card_id,
                usage_snapshot_hash,
            )
        )
        if rating_run_id is None:
            return None
        return self.rating_runs[rating_run_id]

    def insert_rating_run(
        self,
        rating_run: StoredRatingRun,
        rating_lines: tuple[StoredRatingLine, ...],
    ) -> StoredRatingRun:
        """Append an immutable rating run.  Existing identity rows are never updated."""
        identity_key = (
            rating_run.tenant_account_id,
            rating_run.window_started_at,
            rating_run.window_ended_at,
            rating_run.rate_card_id,
            rating_run.usage_snapshot_hash,
        )
        if rating_run.rating_run_id in self.rating_runs:
            raise ValueError("rating runs are immutable and cannot be replaced")
        if identity_key in self.rating_run_index:
            raise ValueError("rating runs are immutable and cannot be replaced")
        persisted = StoredRatingRun(
            rating_run_id=rating_run.rating_run_id,
            tenant_account_id=rating_run.tenant_account_id,
            rate_card_id=rating_run.rate_card_id,
            rate_card_code=rating_run.rate_card_code,
            rate_card_version=rating_run.rate_card_version,
            window_started_at=rating_run.window_started_at,
            window_ended_at=rating_run.window_ended_at,
            usage_snapshot_hash=rating_run.usage_snapshot_hash,
            currency_code=rating_run.currency_code,
            rated_total_amount=rating_run.rated_total_amount,
            recorded_at=rating_run.recorded_at,
            rating_lines=rating_lines,
        )
        self.rating_runs[persisted.rating_run_id] = persisted
        self.rating_run_index[identity_key] = persisted.rating_run_id
        self.rating_lines.extend(rating_lines)
        return persisted

    def get_rating_run(self, rating_run_id: UUID) -> StoredRatingRun | None:
        """Return a stored rating run by internal identifier."""
        return self.rating_runs.get(rating_run_id)

    def find_invoice_draft(
        self, tenant_account_id: UUID, rating_run_id: UUID
    ) -> StoredInvoiceDraft | None:
        """Return the draft for one tenant-scoped rating run, if it exists."""
        invoice_draft_id = self.invoice_draft_index.get((tenant_account_id, rating_run_id))
        if invoice_draft_id is None:
            return None
        return self.invoice_drafts[invoice_draft_id]

    def insert_invoice_draft(
        self,
        invoice_draft: StoredInvoiceDraft,
        invoice_draft_lines: tuple[StoredInvoiceDraftLine, ...],
    ) -> StoredInvoiceDraft:
        """Append an immutable invoice draft.  Existing identity rows are never updated."""
        identity_key = (invoice_draft.tenant_account_id, invoice_draft.rating_run_id)
        if invoice_draft.invoice_draft_id in self.invoice_drafts:
            raise ValueError("invoice drafts are immutable and cannot be replaced")
        if identity_key in self.invoice_draft_index:
            raise ValueError("invoice drafts are immutable and cannot be replaced")
        persisted = StoredInvoiceDraft(
            invoice_draft_id=invoice_draft.invoice_draft_id,
            tenant_account_id=invoice_draft.tenant_account_id,
            rating_run_id=invoice_draft.rating_run_id,
            usage_snapshot_hash=invoice_draft.usage_snapshot_hash,
            currency_code=invoice_draft.currency_code,
            invoice_draft_status=invoice_draft.invoice_draft_status,
            drafted_total_amount=invoice_draft.drafted_total_amount,
            recorded_at=invoice_draft.recorded_at,
            invoice_draft_lines=invoice_draft_lines,
        )
        self.invoice_drafts[persisted.invoice_draft_id] = persisted
        self.invoice_draft_index[identity_key] = persisted.invoice_draft_id
        self.invoice_draft_lines.extend(invoice_draft_lines)
        return persisted

    def list_invoice_drafts(self, tenant_account_id: UUID) -> tuple[StoredInvoiceDraft, ...]:
        """Return invoice drafts limited to one tenant."""
        return tuple(
            invoice_draft
            for invoice_draft in self.invoice_drafts.values()
            if invoice_draft.tenant_account_id == tenant_account_id
        )

    def get_invoice_draft(self, invoice_draft_id: UUID) -> StoredInvoiceDraft | None:
        """Return a stored invoice draft by internal identifier."""
        return self.invoice_drafts.get(invoice_draft_id)

    def find_issued_invoice(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredIssuedInvoice | None:
        """Return the issued snapshot for one tenant draft, if any."""
        issued_invoice_id = self.issued_invoice_index.get(
            (tenant_account_id, invoice_draft_id)
        )
        if issued_invoice_id is None:
            return None
        return self.issued_invoices[issued_invoice_id]

    def get_issued_invoice(self, issued_invoice_id: UUID) -> StoredIssuedInvoice | None:
        """Return one issued invoice by internal identifier, if present."""
        return self.issued_invoices.get(issued_invoice_id)

    def list_issued_invoices_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedInvoice, ...]:
        """Return issued invoices limited to one tenant."""
        return tuple(
            invoice
            for invoice in self.issued_invoices.values()
            if invoice.tenant_account_id == tenant_account_id
        )

    def insert_issued_invoice(
        self,
        issued_invoice: StoredIssuedInvoice,
        issued_invoice_lines: tuple[StoredIssuedInvoiceLine, ...],
    ) -> StoredIssuedInvoice:
        """Append an immutable issued snapshot.  Existing identity rows are never updated."""
        if issued_invoice.issued_invoice_status != "issued":
            raise ValueError("issued invoices are immutable and cannot be replaced")
        exclusive = parse_exact_decimal(
            format_exact_decimal(issued_invoice.tax_exclusive_amount)
        )
        tax_amount = parse_exact_decimal(format_exact_decimal(issued_invoice.tax_amount))
        inclusive = parse_exact_decimal(
            format_exact_decimal(issued_invoice.tax_inclusive_amount)
        )
        if inclusive != exclusive + tax_amount:
            raise ValueError("tax_inclusive_amount must equal exclusive plus tax")
        if issued_invoice.issued_invoice_id in self.issued_invoices:
            raise ValueError("issued invoices are immutable and cannot be replaced")
        identity_key = (issued_invoice.tenant_account_id, issued_invoice.invoice_draft_id)
        if identity_key in self.issued_invoice_index:
            raise ValueError("issued invoices are immutable and cannot be replaced")
        persisted = StoredIssuedInvoice(
            issued_invoice_id=issued_invoice.issued_invoice_id,
            tenant_account_id=issued_invoice.tenant_account_id,
            invoice_draft_id=issued_invoice.invoice_draft_id,
            issued_invoice_contract_version=issued_invoice.issued_invoice_contract_version,
            rating_run_id=issued_invoice.rating_run_id,
            usage_snapshot_hash=issued_invoice.usage_snapshot_hash,
            source_payload_hash=issued_invoice.source_payload_hash,
            currency_code=issued_invoice.currency_code,
            tax_exclusive_amount=exclusive,
            tax_amount=tax_amount,
            tax_inclusive_amount=inclusive,
            issued_invoice_status=issued_invoice.issued_invoice_status,
            issued_at=issued_invoice.issued_at,
            due_at=issued_invoice.due_at,
            issued_invoice_lines=issued_invoice_lines,
        )
        self.issued_invoices[persisted.issued_invoice_id] = persisted
        self.issued_invoice_index[identity_key] = persisted.issued_invoice_id
        self.issued_invoice_lines.extend(issued_invoice_lines)
        return persisted

    def find_issued_credit_note(
        self, tenant_account_id: UUID, credit_adjustment_id: UUID
    ) -> StoredIssuedCreditNote | None:
        """Return the issued credit note for one tenant credit, if any."""
        issued_credit_note_id = self.issued_credit_note_index.get(
            (tenant_account_id, credit_adjustment_id)
        )
        if issued_credit_note_id is None:
            return None
        return self.issued_credit_notes[issued_credit_note_id]

    def get_issued_credit_note(
        self, issued_credit_note_id: UUID
    ) -> StoredIssuedCreditNote | None:
        """Return one issued credit note by internal identifier, if present."""
        return self.issued_credit_notes.get(issued_credit_note_id)

    def list_issued_credit_notes_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedCreditNote, ...]:
        """Return issued credit notes limited to one tenant."""
        return tuple(
            note
            for note in self.issued_credit_notes.values()
            if note.tenant_account_id == tenant_account_id
        )

    def insert_issued_credit_note(
        self, issued_credit_note: StoredIssuedCreditNote
    ) -> StoredIssuedCreditNote:
        """Persist one commercial credit-note snapshot.  Identity collisions fail closed."""
        if issued_credit_note.issued_credit_note_status != "issued":
            raise ValueError("issued_credit_note_status must be issued")
        exclusive = parse_exact_decimal(
            format_exact_decimal(issued_credit_note.tax_exclusive_amount)
        )
        tax_amount = parse_exact_decimal(format_exact_decimal(issued_credit_note.tax_amount))
        inclusive = parse_exact_decimal(
            format_exact_decimal(issued_credit_note.tax_inclusive_amount)
        )
        if exclusive + tax_amount != inclusive:
            raise ValueError("issued credit note totals must sum")
        if issued_credit_note.issued_credit_note_id in self.issued_credit_notes:
            raise ValueError("issued_credit_note_id already stored")
        identity_key = (
            issued_credit_note.tenant_account_id,
            issued_credit_note.credit_adjustment_id,
        )
        if identity_key in self.issued_credit_note_index:
            raise ValueError("issued credit note identity already stored")
        persisted = StoredIssuedCreditNote(
            issued_credit_note_id=issued_credit_note.issued_credit_note_id,
            tenant_account_id=issued_credit_note.tenant_account_id,
            credit_adjustment_id=issued_credit_note.credit_adjustment_id,
            invoice_draft_id=issued_credit_note.invoice_draft_id,
            issued_invoice_id=issued_credit_note.issued_invoice_id,
            issued_credit_note_contract_version=issued_credit_note.issued_credit_note_contract_version,
            credit_adjustment_contract_version=issued_credit_note.credit_adjustment_contract_version,
            credit_reason_code=issued_credit_note.credit_reason_code,
            credit_adjustment_source_payload_hash=(
                issued_credit_note.credit_adjustment_source_payload_hash
            ),
            source_payload_hash=issued_credit_note.source_payload_hash,
            currency_code=issued_credit_note.currency_code,
            tax_exclusive_amount=exclusive,
            tax_amount=tax_amount,
            tax_inclusive_amount=inclusive,
            issued_credit_note_status=issued_credit_note.issued_credit_note_status,
            issued_at=issued_credit_note.issued_at,
        )
        self.issued_credit_notes[persisted.issued_credit_note_id] = persisted
        self.issued_credit_note_index[identity_key] = persisted.issued_credit_note_id
        return persisted

    def find_credit_note_application(
        self, tenant_account_id: UUID, issued_credit_note_id: UUID
    ) -> StoredCreditNoteApplication | None:
        """Return the application for one tenant issued credit note, if any."""
        credit_note_application_id = self.credit_note_application_index.get(
            (tenant_account_id, issued_credit_note_id)
        )
        if credit_note_application_id is None:
            return None
        return self.credit_note_applications[credit_note_application_id]

    def get_credit_note_application(
        self, credit_note_application_id: UUID
    ) -> StoredCreditNoteApplication | None:
        """Return one credit-note application by internal identifier, if present."""
        return self.credit_note_applications.get(credit_note_application_id)

    def list_credit_note_applications_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCreditNoteApplication, ...]:
        """Return credit-note applications limited to one tenant."""
        return tuple(
            application
            for application in self.credit_note_applications.values()
            if application.tenant_account_id == tenant_account_id
        )

    def insert_credit_note_application(
        self, credit_note_application: StoredCreditNoteApplication
    ) -> StoredCreditNoteApplication:
        """Append an immutable application.  Existing identity rows are never updated."""
        if credit_note_application.credit_note_application_status != "applied":
            raise ValueError("credit_note_application_status must be applied")
        applied_amount = parse_exact_decimal(
            format_exact_decimal(credit_note_application.applied_amount)
        )
        if applied_amount <= 0:
            raise ValueError("credit note application amount must be a positive exact decimal")
        if credit_note_application.credit_note_application_id in self.credit_note_applications:
            raise ValueError("credit_note_application_id already stored")
        identity_key = (
            credit_note_application.tenant_account_id,
            credit_note_application.issued_credit_note_id,
        )
        if identity_key in self.credit_note_application_index:
            raise ValueError("credit note applications are immutable and cannot be replaced")
        persisted = StoredCreditNoteApplication(
            credit_note_application_id=credit_note_application.credit_note_application_id,
            tenant_account_id=credit_note_application.tenant_account_id,
            issued_credit_note_id=credit_note_application.issued_credit_note_id,
            collection_case_id=credit_note_application.collection_case_id,
            invoice_draft_id=credit_note_application.invoice_draft_id,
            issued_invoice_id=credit_note_application.issued_invoice_id,
            credit_note_application_contract_version=(
                credit_note_application.credit_note_application_contract_version
            ),
            issued_credit_note_contract_version=(
                credit_note_application.issued_credit_note_contract_version
            ),
            source_payload_hash=credit_note_application.source_payload_hash,
            issued_credit_note_source_payload_hash=(
                credit_note_application.issued_credit_note_source_payload_hash
            ),
            currency_code=credit_note_application.currency_code,
            applied_amount=applied_amount,
            credit_note_application_status=credit_note_application.credit_note_application_status,
            applied_at=credit_note_application.applied_at,
        )
        self.credit_note_applications[persisted.credit_note_application_id] = persisted
        self.credit_note_application_index[identity_key] = persisted.credit_note_application_id
        return persisted

    def find_collection_case_settlement(
        self, tenant_account_id: UUID, collection_case_id: UUID
    ) -> StoredCollectionCaseSettlement | None:
        """Return the settle-when-zero row for one tenant collection case, if any."""
        collection_case_settlement_id = self.collection_case_settlement_index.get(
            (tenant_account_id, collection_case_id)
        )
        if collection_case_settlement_id is None:
            return None
        return self.collection_case_settlements[collection_case_settlement_id]

    def get_collection_case_settlement(
        self, collection_case_settlement_id: UUID
    ) -> StoredCollectionCaseSettlement | None:
        """Return one collection-case settlement by internal identifier, if present."""
        return self.collection_case_settlements.get(collection_case_settlement_id)

    def list_collection_case_settlements_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionCaseSettlement, ...]:
        """Return collection-case settlements limited to one tenant."""
        return tuple(
            settlement
            for settlement in self.collection_case_settlements.values()
            if settlement.tenant_account_id == tenant_account_id
        )

    def insert_collection_case_settlement(
        self, collection_case_settlement: StoredCollectionCaseSettlement
    ) -> StoredCollectionCaseSettlement:
        """Append an immutable settle-when-zero row.  Existing identity rows stay."""
        if collection_case_settlement.collection_case_settlement_status != "settled":
            raise ValueError("collection_case_settlement_status must be settled")
        remaining = parse_exact_decimal(
            format_exact_decimal(collection_case_settlement.remaining_outstanding_amount)
        )
        if remaining != 0:
            raise ValueError("collection case settlement remaining must be exact zero")
        if (
            collection_case_settlement.collection_case_settlement_id
            in self.collection_case_settlements
        ):
            raise ValueError("collection_case_settlement_id already stored")
        identity_key = (
            collection_case_settlement.tenant_account_id,
            collection_case_settlement.collection_case_id,
        )
        if identity_key in self.collection_case_settlement_index:
            raise ValueError("collection case settlements are immutable and cannot be replaced")
        persisted = StoredCollectionCaseSettlement(
            collection_case_settlement_id=collection_case_settlement.collection_case_settlement_id,
            tenant_account_id=collection_case_settlement.tenant_account_id,
            collection_case_id=collection_case_settlement.collection_case_id,
            invoice_draft_id=collection_case_settlement.invoice_draft_id,
            issued_invoice_id=collection_case_settlement.issued_invoice_id,
            collection_case_settlement_contract_version=(
                collection_case_settlement.collection_case_settlement_contract_version
            ),
            source_payload_hash=collection_case_settlement.source_payload_hash,
            currency_code=collection_case_settlement.currency_code,
            remaining_outstanding_amount=remaining,
            collection_case_settlement_status=(
                collection_case_settlement.collection_case_settlement_status
            ),
            settled_at=collection_case_settlement.settled_at,
        )
        self.collection_case_settlements[persisted.collection_case_settlement_id] = persisted
        self.collection_case_settlement_index[identity_key] = (
            persisted.collection_case_settlement_id
        )
        return persisted

    def find_collection_write_off(
        self, tenant_account_id: UUID, collection_case_id: UUID
    ) -> StoredCollectionWriteOff | None:
        """Return the write-off row for one tenant collection case, if any."""
        collection_write_off_id = self.collection_write_off_index.get(
            (tenant_account_id, collection_case_id)
        )
        if collection_write_off_id is None:
            return None
        return self.collection_write_offs[collection_write_off_id]

    def get_collection_write_off(
        self, collection_write_off_id: UUID
    ) -> StoredCollectionWriteOff | None:
        """Return one collection write-off by internal identifier, if present."""
        return self.collection_write_offs.get(collection_write_off_id)

    def list_collection_write_offs_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionWriteOff, ...]:
        """Return collection write-offs limited to one tenant."""
        return tuple(
            write_off
            for write_off in self.collection_write_offs.values()
            if write_off.tenant_account_id == tenant_account_id
        )

    def insert_collection_write_off(
        self, collection_write_off: StoredCollectionWriteOff
    ) -> StoredCollectionWriteOff:
        """Append an immutable write-off row.  Existing identity rows stay."""
        if collection_write_off.collection_write_off_status != "recorded":
            raise ValueError("collection_write_off_status must be recorded")
        remaining = parse_exact_decimal(
            format_exact_decimal(collection_write_off.remaining_outstanding_amount)
        )
        if remaining != 0:
            raise ValueError("collection write-off remaining must be exact zero")
        write_off_amount = parse_exact_decimal(
            format_exact_decimal(collection_write_off.write_off_amount)
        )
        if write_off_amount <= 0:
            raise ValueError("collection write-off amount must be a positive exact decimal")
        if collection_write_off.collection_write_off_id in self.collection_write_offs:
            raise ValueError("collection_write_off_id already stored")
        identity_key = (
            collection_write_off.tenant_account_id,
            collection_write_off.collection_case_id,
        )
        if identity_key in self.collection_write_off_index:
            raise ValueError("collection write-offs are immutable and cannot be replaced")
        persisted = StoredCollectionWriteOff(
            collection_write_off_id=collection_write_off.collection_write_off_id,
            tenant_account_id=collection_write_off.tenant_account_id,
            collection_case_id=collection_write_off.collection_case_id,
            invoice_draft_id=collection_write_off.invoice_draft_id,
            issued_invoice_id=collection_write_off.issued_invoice_id,
            collection_write_off_contract_version=(
                collection_write_off.collection_write_off_contract_version
            ),
            source_payload_hash=collection_write_off.source_payload_hash,
            currency_code=collection_write_off.currency_code,
            write_off_amount=write_off_amount,
            remaining_outstanding_amount=remaining,
            collection_write_off_status=collection_write_off.collection_write_off_status,
            written_off_at=collection_write_off.written_off_at,
        )
        self.collection_write_offs[persisted.collection_write_off_id] = persisted
        self.collection_write_off_index[identity_key] = persisted.collection_write_off_id
        return persisted

    def find_unapplied_cash(
        self, tenant_account_id: UUID, payment_receipt_id: UUID
    ) -> StoredUnappliedCash | None:
        """Return the parked leftover for one tenant payment receipt, if any."""
        unapplied_cash_id = self.unapplied_cash_index.get(
            (tenant_account_id, payment_receipt_id)
        )
        if unapplied_cash_id is None:
            return None
        return self.unapplied_cash[unapplied_cash_id]

    def get_unapplied_cash(self, unapplied_cash_id: UUID) -> StoredUnappliedCash | None:
        """Return one unapplied-cash row by internal identifier, if present."""
        return self.unapplied_cash.get(unapplied_cash_id)

    def list_unapplied_cash_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCash, ...]:
        """Return unapplied-cash rows limited to one tenant."""
        return tuple(
            parked
            for parked in self.unapplied_cash.values()
            if parked.tenant_account_id == tenant_account_id
        )

    def insert_unapplied_cash(self, unapplied_cash: StoredUnappliedCash) -> StoredUnappliedCash:
        """Append an immutable parked leftover.  Existing identity rows stay."""
        if unapplied_cash.unapplied_cash_status != "parked":
            raise ValueError("unapplied_cash_status must be parked")
        leftover = parse_exact_decimal(format_exact_decimal(unapplied_cash.unapplied_amount))
        if leftover <= 0:
            raise ValueError("unapplied cash amount must be a positive exact decimal")
        received_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash.received_amount)
        )
        applied_amount = parse_exact_decimal(format_exact_decimal(unapplied_cash.applied_amount))
        if leftover > received_amount:
            raise ValueError("unapplied cash cannot exceed the stored receipt")
        if unapplied_cash.unapplied_cash_id in self.unapplied_cash:
            raise ValueError("unapplied_cash_id already stored")
        identity_key = (
            unapplied_cash.tenant_account_id,
            unapplied_cash.payment_receipt_id,
        )
        if identity_key in self.unapplied_cash_index:
            raise ValueError("unapplied cash rows are immutable and cannot be replaced")
        persisted = StoredUnappliedCash(
            unapplied_cash_id=unapplied_cash.unapplied_cash_id,
            tenant_account_id=unapplied_cash.tenant_account_id,
            payment_receipt_id=unapplied_cash.payment_receipt_id,
            payment_intent_id=unapplied_cash.payment_intent_id,
            collection_case_id=unapplied_cash.collection_case_id,
            unapplied_cash_contract_version=unapplied_cash.unapplied_cash_contract_version,
            source_payload_hash=unapplied_cash.source_payload_hash,
            currency_code=unapplied_cash.currency_code,
            unapplied_amount=leftover,
            received_amount=received_amount,
            applied_amount=applied_amount,
            unapplied_cash_status=unapplied_cash.unapplied_cash_status,
            parked_at=unapplied_cash.parked_at,
        )
        self.unapplied_cash[persisted.unapplied_cash_id] = persisted
        self.unapplied_cash_index[identity_key] = persisted.unapplied_cash_id
        return persisted

    def find_unapplied_cash_application(
        self, tenant_account_id: UUID, unapplied_cash_id: UUID
    ) -> StoredUnappliedCashApplication | None:
        """Return the application for one tenant parked leftover, if any."""
        unapplied_cash_application_id = self.unapplied_cash_application_index.get(
            (tenant_account_id, unapplied_cash_id)
        )
        if unapplied_cash_application_id is None:
            return None
        return self.unapplied_cash_applications[unapplied_cash_application_id]

    def get_unapplied_cash_application(
        self, unapplied_cash_application_id: UUID
    ) -> StoredUnappliedCashApplication | None:
        """Return one leftover application by internal identifier, if present."""
        return self.unapplied_cash_applications.get(unapplied_cash_application_id)

    def list_unapplied_cash_applications_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCashApplication, ...]:
        """Return leftover applications limited to one tenant."""
        return tuple(
            application
            for application in self.unapplied_cash_applications.values()
            if application.tenant_account_id == tenant_account_id
        )

    def insert_unapplied_cash_application(
        self, unapplied_cash_application: StoredUnappliedCashApplication
    ) -> StoredUnappliedCashApplication:
        """Append an immutable leftover application.  Existing identity stays."""
        if unapplied_cash_application.unapplied_cash_application_status != "applied":
            raise ValueError("unapplied_cash_application_status must be applied")
        applied_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_application.applied_amount)
        )
        if applied_amount <= 0:
            raise ValueError("unapplied cash application amount must be a positive exact decimal")
        if (
            unapplied_cash_application.unapplied_cash_application_id
            in self.unapplied_cash_applications
        ):
            raise ValueError("unapplied_cash_application_id already stored")
        identity_key = (
            unapplied_cash_application.tenant_account_id,
            unapplied_cash_application.unapplied_cash_id,
        )
        if identity_key in self.unapplied_cash_application_index:
            raise ValueError("unapplied cash applications are immutable and cannot be replaced")
        persisted = StoredUnappliedCashApplication(
            unapplied_cash_application_id=unapplied_cash_application.unapplied_cash_application_id,
            tenant_account_id=unapplied_cash_application.tenant_account_id,
            unapplied_cash_id=unapplied_cash_application.unapplied_cash_id,
            collection_case_id=unapplied_cash_application.collection_case_id,
            payment_receipt_id=unapplied_cash_application.payment_receipt_id,
            invoice_draft_id=unapplied_cash_application.invoice_draft_id,
            unapplied_cash_application_contract_version=(
                unapplied_cash_application.unapplied_cash_application_contract_version
            ),
            source_payload_hash=unapplied_cash_application.source_payload_hash,
            currency_code=unapplied_cash_application.currency_code,
            applied_amount=applied_amount,
            unapplied_cash_application_status=(
                unapplied_cash_application.unapplied_cash_application_status
            ),
            applied_at=unapplied_cash_application.applied_at,
        )
        self.unapplied_cash_applications[persisted.unapplied_cash_application_id] = persisted
        self.unapplied_cash_application_index[identity_key] = (
            persisted.unapplied_cash_application_id
        )
        return persisted

    def find_unapplied_cash_refund(
        self, tenant_account_id: UUID, unapplied_cash_id: UUID
    ) -> StoredUnappliedCashRefund | None:
        """Return the refund for one tenant parked leftover, if any."""
        unapplied_cash_refund_id = self.unapplied_cash_refund_index.get(
            (tenant_account_id, unapplied_cash_id)
        )
        if unapplied_cash_refund_id is None:
            return None
        return self.unapplied_cash_refunds[unapplied_cash_refund_id]

    def get_unapplied_cash_refund(
        self, unapplied_cash_refund_id: UUID
    ) -> StoredUnappliedCashRefund | None:
        """Return one leftover refund by internal identifier, if present."""
        return self.unapplied_cash_refunds.get(unapplied_cash_refund_id)

    def list_unapplied_cash_refunds_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCashRefund, ...]:
        """Return leftover refunds limited to one tenant."""
        return tuple(
            refund
            for refund in self.unapplied_cash_refunds.values()
            if refund.tenant_account_id == tenant_account_id
        )

    def insert_unapplied_cash_refund(
        self, unapplied_cash_refund: StoredUnappliedCashRefund
    ) -> StoredUnappliedCashRefund:
        """Append an immutable leftover refund.  Existing identity stays."""
        if unapplied_cash_refund.unapplied_cash_refund_status != "recorded":
            raise ValueError("unapplied_cash_refund_status must be recorded")
        refund_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_refund.refund_amount)
        )
        if refund_amount <= 0:
            raise ValueError("unapplied cash refund amount must be a positive exact decimal")
        unapplied_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_refund.unapplied_amount)
        )
        if unapplied_amount <= 0:
            raise ValueError("unapplied cash amount must be a positive exact decimal")
        if unapplied_cash_refund.unapplied_cash_refund_id in self.unapplied_cash_refunds:
            raise ValueError("unapplied_cash_refund_id already stored")
        identity_key = (
            unapplied_cash_refund.tenant_account_id,
            unapplied_cash_refund.unapplied_cash_id,
        )
        if identity_key in self.unapplied_cash_refund_index:
            raise ValueError("unapplied cash refunds are immutable and cannot be replaced")
        persisted = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=unapplied_cash_refund.unapplied_cash_refund_id,
            tenant_account_id=unapplied_cash_refund.tenant_account_id,
            unapplied_cash_id=unapplied_cash_refund.unapplied_cash_id,
            payment_receipt_id=unapplied_cash_refund.payment_receipt_id,
            payment_intent_id=unapplied_cash_refund.payment_intent_id,
            collection_case_id=unapplied_cash_refund.collection_case_id,
            unapplied_cash_refund_contract_version=(
                unapplied_cash_refund.unapplied_cash_refund_contract_version
            ),
            source_payload_hash=unapplied_cash_refund.source_payload_hash,
            currency_code=unapplied_cash_refund.currency_code,
            refund_amount=refund_amount,
            unapplied_amount=unapplied_amount,
            unapplied_cash_refund_status=unapplied_cash_refund.unapplied_cash_refund_status,
            refunded_at=unapplied_cash_refund.refunded_at,
        )
        self.unapplied_cash_refunds[persisted.unapplied_cash_refund_id] = persisted
        self.unapplied_cash_refund_index[identity_key] = persisted.unapplied_cash_refund_id
        return persisted

    def apply_unapplied_cash_to_collection_case(
        self, collection_case_id: UUID, applied_amount: Decimal
    ) -> StoredCollectionCase:
        """Reduce outstanding by parked leftover without flipping to settled.

        #46 remains the explicit settle-when-zero command.  Status stays
        ``open`` or ``dunning`` even when remaining becomes exact zero.
        """
        stored = self.collection_cases.get(collection_case_id)
        if stored is None:
            raise ValueError("unapplied cash apply requires a stored collection case")
        amount = parse_exact_decimal(format_exact_decimal(applied_amount))
        if amount <= 0:
            raise ValueError("unapplied cash apply amount must be a positive exact decimal")
        if stored.collection_case_status == "settled":
            raise ValueError("settled collection cases cannot accept unapplied cash")
        remaining = parse_exact_decimal(format_exact_decimal(stored.outstanding_amount))
        if amount > remaining:
            raise ValueError("unapplied cash apply amount cannot exceed outstanding")
        updated = StoredCollectionCase(
            collection_case_id=stored.collection_case_id,
            tenant_account_id=stored.tenant_account_id,
            invoice_draft_id=stored.invoice_draft_id,
            currency_code=stored.currency_code,
            collection_case_status=stored.collection_case_status,
            outstanding_amount=remaining - amount,
            opened_at=stored.opened_at,
        )
        self.collection_cases[updated.collection_case_id] = updated
        return updated

    def apply_collection_write_off(
        self, collection_case_id: UUID, write_off_amount: Decimal
    ) -> StoredCollectionCase:
        """Zero leftover outstanding without flipping the case to settled.

        This is the commercial write-off balance update.  #46 remains the
        explicit settle-when-zero command.  Status stays ``open`` or
        ``dunning``.
        """
        stored = self.collection_cases.get(collection_case_id)
        if stored is None:
            raise ValueError("collection write-off requires a stored collection case")
        amount = parse_exact_decimal(format_exact_decimal(write_off_amount))
        if amount <= 0:
            raise ValueError("collection write-off amount must be a positive exact decimal")
        if stored.collection_case_status == "settled":
            raise ValueError("settled collection cases cannot accept a write-off")
        remaining = parse_exact_decimal(format_exact_decimal(stored.outstanding_amount))
        if remaining != amount:
            raise ValueError("collection write-off amount must equal remaining outstanding")
        updated = StoredCollectionCase(
            collection_case_id=stored.collection_case_id,
            tenant_account_id=stored.tenant_account_id,
            invoice_draft_id=stored.invoice_draft_id,
            currency_code=stored.currency_code,
            collection_case_status=stored.collection_case_status,
            outstanding_amount=Decimal("0"),
            opened_at=stored.opened_at,
        )
        self.collection_cases[updated.collection_case_id] = updated
        return updated

    def mark_collection_case_settled(self, collection_case_id: UUID) -> StoredCollectionCase:
        """Flip an exact-zero case to ``settled`` without changing outstanding.

        This is the explicit settle command.  Receipt and credit-note apply
        paths still settle through ``apply_collection_settlement``.
        """
        stored = self.collection_cases.get(collection_case_id)
        if stored is None:
            raise ValueError("collection settlement requires a stored collection case")
        remaining = parse_exact_decimal(format_exact_decimal(stored.outstanding_amount))
        if remaining != 0:
            raise ValueError("collection case outstanding must be exact zero to settle")
        if stored.collection_case_status == "settled":
            return stored
        updated = StoredCollectionCase(
            collection_case_id=stored.collection_case_id,
            tenant_account_id=stored.tenant_account_id,
            invoice_draft_id=stored.invoice_draft_id,
            currency_code=stored.currency_code,
            collection_case_status="settled",
            outstanding_amount=Decimal("0"),
            opened_at=stored.opened_at,
        )
        self.collection_cases[updated.collection_case_id] = updated
        return updated

    def find_credit_adjustment(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        source_payload_hash: str,
        credit_adjustment_contract_version: int,
    ) -> StoredCreditAdjustment | None:
        """Return the credit adjustment for one tenant-scoped identity, if any."""
        credit_adjustment_id = self.credit_adjustment_index.get(
            (
                tenant_account_id,
                invoice_draft_id,
                source_payload_hash,
                credit_adjustment_contract_version,
            )
        )
        if credit_adjustment_id is None:
            return None
        return self.credit_adjustments[credit_adjustment_id]

    def get_credit_adjustment(
        self, credit_adjustment_id: UUID
    ) -> StoredCreditAdjustment | None:
        """Return one credit adjustment by internal identifier, if present."""
        return self.credit_adjustments.get(credit_adjustment_id)

    def insert_credit_adjustment(
        self, credit: StoredCreditAdjustment
    ) -> StoredCreditAdjustment:
        """Persist one credit adjustment or return the existing identity row.

        The same tenant-scoped identity is a duplicate replay.  Reusing
        ``credit_adjustment_id`` for a different identity is rejected so a
        provider or AIS identifier cannot become the internal key.
        """
        if credit.credit_reason_code not in CREDIT_REASON_CODES:
            raise ValueError("credit_reason_code is not in the closed set")
        credit_amount = parse_exact_decimal(format_exact_decimal(credit.credit_amount))
        if credit_amount <= 0:
            raise ValueError("credit amount must be a positive exact decimal")
        tax_exclusive_amount = parse_exact_decimal(
            format_exact_decimal(credit.tax_exclusive_amount)
        )
        tax_amount = parse_exact_decimal(format_exact_decimal(credit.tax_amount))
        if tax_exclusive_amount + tax_amount != credit_amount:
            raise ValueError("credit tax split must sum to credit_amount")
        existing_by_id = self.credit_adjustments.get(credit.credit_adjustment_id)
        if existing_by_id is not None:
            if (
                existing_by_id.tenant_account_id != credit.tenant_account_id
                or existing_by_id.invoice_draft_id != credit.invoice_draft_id
                or existing_by_id.source_payload_hash != credit.source_payload_hash
                or existing_by_id.credit_adjustment_contract_version
                != credit.credit_adjustment_contract_version
            ):
                raise ValueError(
                    "credit_adjustment_id already stored for a different identity"
                )
            return existing_by_id
        identity = (
            credit.tenant_account_id,
            credit.invoice_draft_id,
            credit.source_payload_hash,
            credit.credit_adjustment_contract_version,
        )
        existing_id = self.credit_adjustment_index.get(identity)
        if existing_id is not None:
            return self.credit_adjustments[existing_id]
        persisted = StoredCreditAdjustment(
            credit_adjustment_id=credit.credit_adjustment_id,
            tenant_account_id=credit.tenant_account_id,
            invoice_draft_id=credit.invoice_draft_id,
            credit_adjustment_contract_version=credit.credit_adjustment_contract_version,
            credit_reason_code=credit.credit_reason_code,
            currency_code=credit.currency_code,
            credit_amount=credit_amount,
            tax_exclusive_amount=tax_exclusive_amount,
            tax_amount=tax_amount,
            source_payload_hash=credit.source_payload_hash,
            recorded_at=credit.recorded_at,
        )
        self.credit_adjustments[persisted.credit_adjustment_id] = persisted
        self.credit_adjustment_index[identity] = persisted.credit_adjustment_id
        return persisted

    def list_credit_adjustments(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredCreditAdjustment, ...]:
        """Return stored credit adjustments, optionally filtered by tenant."""
        credits = tuple(self.credit_adjustments.values())
        if tenant_account_id is None:
            return credits
        return tuple(
            credit
            for credit in credits
            if credit.tenant_account_id == tenant_account_id
        )

    def find_journal_proposal(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return the proposal for one tenant-scoped draft identity, if it exists."""
        journal_proposal_id = self.journal_proposal_index.get(
            (
                tenant_account_id,
                invoice_draft_id,
                source_payload_hash,
                proposal_contract_version,
            )
        )
        if journal_proposal_id is None:
            return None
        return self.journal_proposals[journal_proposal_id]

    def find_journal_proposal_for_receipt(
        self,
        tenant_account_id: UUID,
        payment_receipt_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return the cash proposal for one tenant-scoped receipt, if it exists."""
        journal_proposal_id = self.cash_journal_proposal_index.get(
            (
                tenant_account_id,
                payment_receipt_id,
                source_payload_hash,
                proposal_contract_version,
            )
        )
        if journal_proposal_id is None:
            return None
        return self.journal_proposals[journal_proposal_id]

    def find_journal_proposal_for_credit(
        self,
        tenant_account_id: UUID,
        credit_adjustment_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return the credit proposal for one tenant-scoped credit, if it exists."""
        journal_proposal_id = self.credit_journal_proposal_index.get(
            (
                tenant_account_id,
                credit_adjustment_id,
                source_payload_hash,
                proposal_contract_version,
            )
        )
        if journal_proposal_id is None:
            return None
        return self.journal_proposals[journal_proposal_id]

    def find_journal_proposal_for_credit_adjustment(
        self,
        tenant_account_id: UUID,
        credit_adjustment_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return the credit proposal for one tenant-scoped credit, if it exists."""
        journal_proposal_id = self.credit_journal_adjustment_index.get(
            (tenant_account_id, credit_adjustment_id)
        )
        if journal_proposal_id is None:
            return None
        return self.journal_proposals[journal_proposal_id]

    def find_journal_proposal_for_write_off(
        self,
        tenant_account_id: UUID,
        collection_write_off_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return the write-off proposal for one tenant-scoped write-off, if it exists."""
        journal_proposal_id = self.write_off_journal_proposal_index.get(
            (tenant_account_id, collection_write_off_id)
        )
        if journal_proposal_id is None:
            return None
        return self.journal_proposals[journal_proposal_id]

    def insert_journal_proposal(
        self,
        journal_proposal: StoredJournalProposal,
        proposal_lines: tuple[StoredJournalProposalLine, ...],
    ) -> StoredJournalProposal:
        """Append an immutable balanced proposal.  Existing identity rows are never updated."""
        if journal_proposal.proposal_status not in {"draft", "validated", "exported", "rejected"}:
            raise ValueError("journal proposals cannot be posted")
        line_numbers = [line.line_number for line in proposal_lines]
        if len(set(line_numbers)) != len(line_numbers):
            raise ValueError("journal proposal line numbers must be unique")
        parsed_lines = tuple(
            StoredJournalProposalLine(
                journal_proposal_line_id=line.journal_proposal_line_id,
                journal_proposal_id=line.journal_proposal_id,
                tenant_account_id=line.tenant_account_id,
                line_number=line.line_number,
                account_role_code=line.account_role_code,
                debit_amount=parse_exact_decimal(format_exact_decimal(line.debit_amount)),
                credit_amount=parse_exact_decimal(format_exact_decimal(line.credit_amount)),
            )
            for line in proposal_lines
        )
        for line in parsed_lines:
            debit_positive = line.debit_amount > 0
            credit_positive = line.credit_amount > 0
            if debit_positive == credit_positive:
                raise ValueError("journal proposal lines must be debit XOR credit")
        debit_total = sum((line.debit_amount for line in parsed_lines), Decimal("0"))
        credit_total = sum((line.credit_amount for line in parsed_lines), Decimal("0"))
        if debit_total != credit_total:
            raise ValueError("journal proposal lines must balance")
        identity_key = (
            journal_proposal.tenant_account_id,
            journal_proposal.invoice_draft_id,
            journal_proposal.source_payload_hash,
            journal_proposal.proposal_contract_version,
        )
        cash_identity_key = None
        if journal_proposal.payment_receipt_id is not None:
            cash_identity_key = (
                journal_proposal.tenant_account_id,
                journal_proposal.payment_receipt_id,
                journal_proposal.source_payload_hash,
                journal_proposal.proposal_contract_version,
            )
        credit_identity_key = None
        credit_adjustment_only_key = None
        if journal_proposal.credit_adjustment_id is not None:
            credit_identity_key = (
                journal_proposal.tenant_account_id,
                journal_proposal.credit_adjustment_id,
                journal_proposal.source_payload_hash,
                journal_proposal.proposal_contract_version,
            )
            credit_adjustment_only_key = (
                journal_proposal.tenant_account_id,
                journal_proposal.credit_adjustment_id,
            )
        write_off_identity_key = None
        if journal_proposal.collection_write_off_id is not None:
            write_off_identity_key = (
                journal_proposal.tenant_account_id,
                journal_proposal.collection_write_off_id,
            )
        if journal_proposal.journal_proposal_id in self.journal_proposals:
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if identity_key in self.journal_proposal_index:
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if cash_identity_key is not None and cash_identity_key in self.cash_journal_proposal_index:
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if (
            credit_identity_key is not None
            and credit_identity_key in self.credit_journal_proposal_index
        ):
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if (
            credit_adjustment_only_key is not None
            and credit_adjustment_only_key in self.credit_journal_adjustment_index
        ):
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if (
            write_off_identity_key is not None
            and write_off_identity_key in self.write_off_journal_proposal_index
        ):
            raise ValueError("journal proposals are immutable and cannot be replaced")
        persisted = StoredJournalProposal(
            journal_proposal_id=journal_proposal.journal_proposal_id,
            tenant_account_id=journal_proposal.tenant_account_id,
            invoice_draft_id=journal_proposal.invoice_draft_id,
            proposal_contract_version=journal_proposal.proposal_contract_version,
            idempotency_key=journal_proposal.idempotency_key,
            legal_entity_reference=journal_proposal.legal_entity_reference,
            intended_book_role_code=journal_proposal.intended_book_role_code,
            transaction_currency=journal_proposal.transaction_currency,
            transaction_date=journal_proposal.transaction_date,
            accounting_date=journal_proposal.accounting_date,
            source_payload_hash=journal_proposal.source_payload_hash,
            proposed_at=journal_proposal.proposed_at,
            proposal_status=journal_proposal.proposal_status,
            source_event_reference=journal_proposal.source_event_reference,
            proposal_lines=parsed_lines,
            payment_receipt_id=journal_proposal.payment_receipt_id,
            credit_adjustment_id=journal_proposal.credit_adjustment_id,
            collection_write_off_id=journal_proposal.collection_write_off_id,
        )
        self.journal_proposals[persisted.journal_proposal_id] = persisted
        self.journal_proposal_index[identity_key] = persisted.journal_proposal_id
        if cash_identity_key is not None:
            self.cash_journal_proposal_index[cash_identity_key] = persisted.journal_proposal_id
        if credit_identity_key is not None:
            self.credit_journal_proposal_index[credit_identity_key] = persisted.journal_proposal_id
        if credit_adjustment_only_key is not None:
            self.credit_journal_adjustment_index[credit_adjustment_only_key] = (
                persisted.journal_proposal_id
            )
        if write_off_identity_key is not None:
            self.write_off_journal_proposal_index[write_off_identity_key] = (
                persisted.journal_proposal_id
            )
        self.journal_proposal_lines.extend(parsed_lines)
        return persisted

    def list_journal_proposals(self, tenant_account_id: UUID) -> tuple[StoredJournalProposal, ...]:
        """Return journal proposals limited to one tenant."""
        return tuple(
            proposal
            for proposal in self.journal_proposals.values()
            if proposal.tenant_account_id == tenant_account_id
        )

    def get_journal_proposal(self, journal_proposal_id: UUID) -> StoredJournalProposal | None:
        """Return a stored journal proposal by internal identifier."""
        return self.journal_proposals.get(journal_proposal_id)

    def get_collection_case(self, collection_case_id: UUID) -> StoredCollectionCase | None:
        """Return a stored collection case by internal identifier."""
        return self.collection_cases.get(collection_case_id)

    def find_collection_case(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredCollectionCase | None:
        """Return the case for one tenant-scoped invoice draft, if it exists."""
        collection_case_id = self.collection_case_index.get((tenant_account_id, invoice_draft_id))
        if collection_case_id is None:
            return None
        return self.collection_cases[collection_case_id]

    def insert_collection_case(self, collection_case: StoredCollectionCase) -> StoredCollectionCase:
        """Append an immutable collection case.  Existing identity rows are never updated."""
        if collection_case.collection_case_status not in {"open", "dunning"}:
            raise ValueError("collection cases cannot be paid, written off, or posted")
        outstanding_amount = parse_exact_decimal(
            format_exact_decimal(collection_case.outstanding_amount)
        )
        if outstanding_amount <= 0:
            raise ValueError("collection case outstanding must be a positive exact decimal")
        identity_key = (collection_case.tenant_account_id, collection_case.invoice_draft_id)
        if collection_case.collection_case_id in self.collection_cases:
            raise ValueError("collection cases are immutable and cannot be replaced")
        if identity_key in self.collection_case_index:
            raise ValueError("collection cases are immutable and cannot be replaced")
        persisted = StoredCollectionCase(
            collection_case_id=collection_case.collection_case_id,
            tenant_account_id=collection_case.tenant_account_id,
            invoice_draft_id=collection_case.invoice_draft_id,
            currency_code=collection_case.currency_code,
            collection_case_status=collection_case.collection_case_status,
            outstanding_amount=outstanding_amount,
            opened_at=collection_case.opened_at,
        )
        self.collection_cases[persisted.collection_case_id] = persisted
        self.collection_case_index[identity_key] = persisted.collection_case_id
        return persisted

    def list_collection_cases(self, tenant_account_id: UUID) -> tuple[StoredCollectionCase, ...]:
        """Return collection cases limited to one tenant."""
        return tuple(
            collection_case
            for collection_case in self.collection_cases.values()
            if collection_case.tenant_account_id == tenant_account_id
        )

    def get_collection_dunning_event(
        self, collection_dunning_event_id: UUID
    ) -> StoredCollectionDunningEvent | None:
        """Return one collection dunning event by internal identifier, if present."""
        for event in self.collection_dunning_events:
            if event.collection_dunning_event_id == collection_dunning_event_id:
                return event
        return None

    def list_collection_dunning_events(
        self, collection_case_id: UUID
    ) -> tuple[StoredCollectionDunningEvent, ...]:
        """Return dunning events for one collection case in event-number order."""
        matched = [
            event
            for event in self.collection_dunning_events
            if event.collection_case_id == collection_case_id
        ]
        return tuple(sorted(matched, key=lambda event: event.dunning_event_number))

    def list_collection_dunning_events_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionDunningEvent, ...]:
        """Return dunning events limited to one tenant."""
        return tuple(
            event
            for event in self.collection_dunning_events
            if event.tenant_account_id == tenant_account_id
        )

    def find_collection_dunning_event(
        self, collection_case_id: UUID, dunning_notice_code: str
    ) -> StoredCollectionDunningEvent | None:
        """Return the stored reminder for one case and notice code, if it exists."""
        for event in self.collection_dunning_events:
            if (
                event.collection_case_id == collection_case_id
                and event.dunning_notice_code == dunning_notice_code
            ):
                return event
        return None

    def insert_collection_dunning_event(
        self, dunning_event: StoredCollectionDunningEvent
    ) -> StoredCollectionDunningEvent:
        """Append an immutable commercial reminder.  Existing notice rows are never updated."""
        if dunning_event.dunning_notice_code not in {"first_notice", "overdue_notice"}:
            raise ValueError("collection dunning notices must be commercial reminder codes")
        if dunning_event.collection_case_id not in self.collection_cases:
            raise ValueError("collection dunning events require a stored collection case")
        notice_key = (dunning_event.collection_case_id, dunning_event.dunning_notice_code)
        number_key = (dunning_event.collection_case_id, dunning_event.dunning_event_number)
        if dunning_event.collection_dunning_event_id in {
            event.collection_dunning_event_id for event in self.collection_dunning_events
        }:
            raise ValueError("collection dunning events are immutable and cannot be replaced")
        if notice_key in self.collection_dunning_notice_index:
            raise ValueError("collection dunning events are immutable and cannot be replaced")
        if number_key in self.collection_dunning_number_index:
            raise ValueError("collection dunning events are immutable and cannot be replaced")
        self.collection_dunning_events.append(dunning_event)
        self.collection_dunning_notice_index[notice_key] = dunning_event.collection_dunning_event_id
        self.collection_dunning_number_index[number_key] = dunning_event.collection_dunning_event_id
        return dunning_event

    def get_payment_intent(self, payment_intent_id: UUID) -> StoredPaymentIntent | None:
        """Return a stored payment intent by internal identifier."""
        return self.payment_intents.get(payment_intent_id)

    def find_payment_intent(
        self,
        tenant_account_id: UUID,
        collection_case_id: UUID,
        source_payload_hash: str,
        payment_intent_contract_version: int,
    ) -> StoredPaymentIntent | None:
        """Return the intent for one tenant-scoped case snapshot, if it exists."""
        payment_intent_id = self.payment_intent_index.get(
            (
                tenant_account_id,
                collection_case_id,
                source_payload_hash,
                payment_intent_contract_version,
            )
        )
        if payment_intent_id is None:
            return None
        return self.payment_intents[payment_intent_id]

    def insert_payment_intent(self, payment_intent: StoredPaymentIntent) -> StoredPaymentIntent:
        """Append an immutable payment intent.  Existing identity rows are never updated."""
        if payment_intent.payment_intent_status not in {"projected", "cancelled", "rejected"}:
            raise ValueError("payment intents cannot be captured, settled, or posted")
        payment_amount = parse_exact_decimal(format_exact_decimal(payment_intent.payment_amount))
        if payment_amount <= 0:
            raise ValueError("payment intent amount must be a positive exact decimal")
        identity_key = (
            payment_intent.tenant_account_id,
            payment_intent.collection_case_id,
            payment_intent.source_payload_hash,
            payment_intent.payment_intent_contract_version,
        )
        if payment_intent.payment_intent_id in self.payment_intents:
            raise ValueError("payment intents are immutable and cannot be replaced")
        if identity_key in self.payment_intent_index:
            raise ValueError("payment intents are immutable and cannot be replaced")
        persisted = StoredPaymentIntent(
            payment_intent_id=payment_intent.payment_intent_id,
            tenant_account_id=payment_intent.tenant_account_id,
            collection_case_id=payment_intent.collection_case_id,
            payment_intent_contract_version=payment_intent.payment_intent_contract_version,
            currency_code=payment_intent.currency_code,
            payment_intent_status=payment_intent.payment_intent_status,
            payment_amount=payment_amount,
            source_payload_hash=payment_intent.source_payload_hash,
            projected_at=payment_intent.projected_at,
        )
        self.payment_intents[persisted.payment_intent_id] = persisted
        self.payment_intent_index[identity_key] = persisted.payment_intent_id
        return persisted

    def list_payment_intents(self, tenant_account_id: UUID) -> tuple[StoredPaymentIntent, ...]:
        """Return payment intents limited to one tenant."""
        return tuple(
            payment_intent
            for payment_intent in self.payment_intents.values()
            if payment_intent.tenant_account_id == tenant_account_id
        )

    def apply_collection_settlement(
        self, collection_case_id: UUID, applied_amount: Decimal
    ) -> StoredCollectionCase:
        """Reduce case outstanding by an applied receipt amount.

        Remaining zero marks the current case ``settled``.  Receipts remain the
        immutable history; this method updates the current commercial balance.
        """
        stored = self.collection_cases.get(collection_case_id)
        if stored is None:
            raise ValueError("collection settlement requires a stored collection case")
        applied = parse_exact_decimal(format_exact_decimal(applied_amount))
        if applied <= 0:
            raise ValueError("collection settlement amount must be a positive exact decimal")
        if applied > stored.outstanding_amount:
            raise ValueError("collection settlement amount cannot exceed outstanding")
        remaining_amount = stored.outstanding_amount - applied
        updated = StoredCollectionCase(
            collection_case_id=stored.collection_case_id,
            tenant_account_id=stored.tenant_account_id,
            invoice_draft_id=stored.invoice_draft_id,
            currency_code=stored.currency_code,
            collection_case_status=(
                "settled" if remaining_amount == 0 else stored.collection_case_status
            ),
            outstanding_amount=remaining_amount,
            opened_at=stored.opened_at,
        )
        self.collection_cases[updated.collection_case_id] = updated
        return updated

    def cancel_stored_payment_intent(self, payment_intent_id: UUID) -> StoredPaymentIntent:
        """Flip a projected intent to cancelled without writing a receipt."""
        stored = self.payment_intents.get(payment_intent_id)
        if stored is None:
            raise ValueError("payment intent cancellation requires a stored payment intent")
        if stored.payment_intent_status == "cancelled":
            return stored
        if stored.payment_intent_status != "projected":
            raise ValueError("only projected payment intents can be cancelled")
        updated = StoredPaymentIntent(
            payment_intent_id=stored.payment_intent_id,
            tenant_account_id=stored.tenant_account_id,
            collection_case_id=stored.collection_case_id,
            payment_intent_contract_version=stored.payment_intent_contract_version,
            currency_code=stored.currency_code,
            payment_intent_status="cancelled",
            payment_amount=stored.payment_amount,
            source_payload_hash=stored.source_payload_hash,
            projected_at=stored.projected_at,
        )
        self.payment_intents[updated.payment_intent_id] = updated
        return updated

    def get_payment_receipt(self, payment_receipt_id: UUID) -> StoredPaymentReceipt | None:
        """Return a stored payment receipt by internal identifier."""
        return self.payment_receipts.get(payment_receipt_id)

    def find_payment_receipt(
        self,
        tenant_account_id: UUID,
        payment_intent_id: UUID,
        source_payload_hash: str,
        settlement_contract_version: int,
    ) -> StoredPaymentReceipt | None:
        """Return the receipt for one tenant-scoped intent snapshot, if it exists."""
        payment_receipt_id = self.payment_receipt_index.get(
            (
                tenant_account_id,
                payment_intent_id,
                source_payload_hash,
                settlement_contract_version,
            )
        )
        if payment_receipt_id is None:
            return None
        return self.payment_receipts[payment_receipt_id]

    def insert_payment_receipt(self, payment_receipt: StoredPaymentReceipt) -> StoredPaymentReceipt:
        """Append an immutable applied receipt.  Existing identity rows are never updated."""
        if payment_receipt.payment_receipt_status != "applied":
            raise ValueError("payment receipts cannot be captured or posted")
        received_amount = parse_exact_decimal(format_exact_decimal(payment_receipt.received_amount))
        if received_amount <= 0:
            raise ValueError("payment receipt amount must be a positive exact decimal")
        identity_key = (
            payment_receipt.tenant_account_id,
            payment_receipt.payment_intent_id,
            payment_receipt.source_payload_hash,
            payment_receipt.settlement_contract_version,
        )
        if payment_receipt.payment_receipt_id in self.payment_receipts:
            raise ValueError("payment receipts are immutable and cannot be replaced")
        if identity_key in self.payment_receipt_index:
            raise ValueError("payment receipts are immutable and cannot be replaced")
        persisted = StoredPaymentReceipt(
            payment_receipt_id=payment_receipt.payment_receipt_id,
            tenant_account_id=payment_receipt.tenant_account_id,
            payment_intent_id=payment_receipt.payment_intent_id,
            collection_case_id=payment_receipt.collection_case_id,
            settlement_contract_version=payment_receipt.settlement_contract_version,
            currency_code=payment_receipt.currency_code,
            payment_receipt_status=payment_receipt.payment_receipt_status,
            received_amount=received_amount,
            source_payload_hash=payment_receipt.source_payload_hash,
            received_at=payment_receipt.received_at,
        )
        self.payment_receipts[persisted.payment_receipt_id] = persisted
        self.payment_receipt_index[identity_key] = persisted.payment_receipt_id
        return persisted

    def find_posting_receipt_observation(
        self, tenant_account_id: UUID, idempotency_key: str
    ) -> StoredPostingReceiptObservation | None:
        """Return the observation for one tenant-scoped AIS idempotency key."""
        observation_id = self.posting_receipt_observation_index.get(
            (tenant_account_id, idempotency_key)
        )
        if observation_id is None:
            return None
        return self.posting_receipt_observations[observation_id]

    def find_posting_receipt_observation_by_receipt(
        self, tenant_account_id: UUID, receipt_id: UUID
    ) -> StoredPostingReceiptObservation | None:
        """Return the observation for one tenant-scoped AIS receipt identifier."""
        observation_id = self.posting_receipt_observation_receipt_index.get(
            (tenant_account_id, receipt_id)
        )
        if observation_id is None:
            return None
        return self.posting_receipt_observations[observation_id]

    def insert_posting_receipt_observation(
        self, observation: StoredPostingReceiptObservation
    ) -> StoredPostingReceiptObservation:
        """Append an immutable observation.  Same receipt identity is a replay."""
        if observation.posting_status_code not in {"posted", "held", "rejected", "reversed"}:
            raise ValueError("posting_status_code must remain an AIS-owned receipt status")
        identity_key = (observation.tenant_account_id, observation.idempotency_key)
        receipt_key = (observation.tenant_account_id, observation.receipt_id)
        existing_id = self.posting_receipt_observation_index.get(identity_key)
        if existing_id is not None:
            existing = self.posting_receipt_observations[existing_id]
            if (
                existing.receipt_id == observation.receipt_id
                and existing.source_payload_hash == observation.source_payload_hash
            ):
                return existing
            raise ValueError("posting receipt observations are immutable and cannot be replaced")
        existing_receipt_id = self.posting_receipt_observation_receipt_index.get(receipt_key)
        if existing_receipt_id is not None:
            raise ValueError("posting receipt observations are immutable and cannot be replaced")
        if observation.posting_receipt_observation_id in self.posting_receipt_observations:
            raise ValueError("posting receipt observations are immutable and cannot be replaced")
        self.posting_receipt_observations[observation.posting_receipt_observation_id] = observation
        self.posting_receipt_observation_index[identity_key] = (
            observation.posting_receipt_observation_id
        )
        self.posting_receipt_observation_receipt_index[receipt_key] = (
            observation.posting_receipt_observation_id
        )
        return observation

    def list_posting_receipt_observations(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredPostingReceiptObservation, ...]:
        """Return observations, optionally limited to one tenant."""
        if tenant_account_id is None:
            return tuple(self.posting_receipt_observations.values())
        return tuple(
            observation
            for observation in self.posting_receipt_observations.values()
            if observation.tenant_account_id == tenant_account_id
        )

    def list_payment_receipts(self, tenant_account_id: UUID) -> tuple[StoredPaymentReceipt, ...]:
        """Return payment receipts limited to one tenant."""
        return tuple(
            payment_receipt
            for payment_receipt in self.payment_receipts.values()
            if payment_receipt.tenant_account_id == tenant_account_id
        )

    def list_rating_runs(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredRatingRun, ...]:
        """Return stored rating runs, optionally filtered by tenant."""
        runs = tuple(self.rating_runs.values())
        if tenant_account_id is None:
            return runs
        return tuple(
            rating_run
            for rating_run in runs
            if rating_run.tenant_account_id == tenant_account_id
        )

    def insert_tenant_api_credential(
        self, credential: StoredTenantApiCredential
    ) -> StoredTenantApiCredential:
        """Persist one API credential.  Secrets are never replayed or stored."""
        if credential.credential_status not in {"active", "revoked"}:
            raise ValueError("credential_status must be active or revoked")
        if not credential.credential_secret_hash.startswith("hmac-sha256:"):
            raise ValueError("credential_secret_hash must be a keyed HMAC")
        if credential.tenant_api_credential_id in self.tenant_api_credentials:
            raise ValueError("tenant_api_credential_id already stored")
        if credential.credential_secret_hash in self.tenant_api_credential_hash_index:
            raise ValueError("credential_secret_hash already stored")
        self.tenant_api_credentials[credential.tenant_api_credential_id] = credential
        self.tenant_api_credential_hash_index[credential.credential_secret_hash] = (
            credential.tenant_api_credential_id
        )
        return credential

    def get_tenant_api_credential(
        self, tenant_api_credential_id: UUID
    ) -> StoredTenantApiCredential | None:
        """Return one API credential by internal identifier, if present."""
        return self.tenant_api_credentials.get(tenant_api_credential_id)

    def find_tenant_api_credential_by_hash(
        self, credential_secret_hash: str
    ) -> StoredTenantApiCredential | None:
        """Return the credential for one keyed hash, if present."""
        credential_id = self.tenant_api_credential_hash_index.get(credential_secret_hash)
        if credential_id is None:
            return None
        return self.tenant_api_credentials[credential_id]

    def list_tenant_api_credentials(
        self, tenant_account_id: UUID
    ) -> tuple[StoredTenantApiCredential, ...]:
        """Return API credentials limited to one tenant."""
        return tuple(
            credential
            for credential in self.tenant_api_credentials.values()
            if credential.tenant_account_id == tenant_account_id
        )

    def list_active_tenant_api_credentials(
        self, tenant_account_id: UUID
    ) -> tuple[StoredTenantApiCredential, ...]:
        """Return active API credentials limited to one tenant."""
        return tuple(
            credential
            for credential in self.list_tenant_api_credentials(tenant_account_id)
            if credential.credential_status == "active"
        )

    def revoke_tenant_api_credential(
        self, tenant_api_credential_id: UUID, revoked_at: datetime
    ) -> StoredTenantApiCredential:
        """Mark one stored credential revoked.  A second revoke is idempotent."""
        stored = self.tenant_api_credentials.get(tenant_api_credential_id)
        if stored is None:
            raise ValueError("api credential revocation requires a stored credential")
        if stored.credential_status == "revoked":
            return stored
        updated = StoredTenantApiCredential(
            tenant_api_credential_id=stored.tenant_api_credential_id,
            tenant_account_id=stored.tenant_account_id,
            tenant_api_credential_contract_version=stored.tenant_api_credential_contract_version,
            credential_label=stored.credential_label,
            credential_prefix=stored.credential_prefix,
            credential_secret_hash=stored.credential_secret_hash,
            credential_status="revoked",
            issued_at=stored.issued_at,
            revoked_at=revoked_at,
        )
        self.tenant_api_credentials[updated.tenant_api_credential_id] = updated
        return updated

    def insert_webhook_subscription(
        self, subscription: StoredWebhookSubscription
    ) -> StoredWebhookSubscription:
        """Persist one webhook subscription.  Secrets stay in the process vault."""
        if subscription.subscription_status not in {"active", "revoked"}:
            raise ValueError("subscription_status must be active or revoked")
        if not subscription.webhook_secret_hash.startswith("hmac-sha256:"):
            raise ValueError("webhook_secret_hash must be a keyed HMAC")
        if subscription.webhook_subscription_id in self.webhook_subscriptions:
            raise ValueError("webhook_subscription_id already stored")
        if subscription.webhook_secret_hash in self.webhook_subscription_hash_index:
            raise ValueError("webhook_secret_hash already stored")
        identity = (
            subscription.tenant_account_id,
            subscription.callback_url,
            subscription.event_type_set,
            subscription.webhook_subscription_contract_version,
        )
        if identity in self.webhook_subscription_identity_index:
            raise ValueError("webhook subscription identity already stored")
        self.webhook_subscriptions[subscription.webhook_subscription_id] = subscription
        self.webhook_subscription_identity_index[identity] = subscription.webhook_subscription_id
        self.webhook_subscription_hash_index[subscription.webhook_secret_hash] = (
            subscription.webhook_subscription_id
        )
        return subscription

    def store_webhook_subscription_secret(
        self, webhook_subscription_id: UUID, webhook_secret: str
    ) -> None:
        """Keep the minted secret in process memory.  SQL never stores it."""
        if not webhook_secret:
            raise ValueError("webhook secret must be a non-empty string")
        self.webhook_subscription_secrets[webhook_subscription_id] = webhook_secret

    def get_webhook_subscription_secret(self, webhook_subscription_id: UUID) -> str | None:
        """Return the process-local secret for one subscription, if present."""
        return self.webhook_subscription_secrets.get(webhook_subscription_id)

    def get_webhook_subscription(
        self, webhook_subscription_id: UUID
    ) -> StoredWebhookSubscription | None:
        """Return one webhook subscription by internal identifier, if present."""
        return self.webhook_subscriptions.get(webhook_subscription_id)

    def find_webhook_subscription(
        self,
        tenant_account_id: UUID,
        callback_url: str,
        event_type_set: str,
        webhook_subscription_contract_version: int,
    ) -> StoredWebhookSubscription | None:
        """Return the subscription for one tenant, URL, event set, and version."""
        subscription_id = self.webhook_subscription_identity_index.get(
            (
                tenant_account_id,
                callback_url,
                event_type_set,
                webhook_subscription_contract_version,
            )
        )
        if subscription_id is None:
            return None
        return self.webhook_subscriptions[subscription_id]

    def list_webhook_subscriptions(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookSubscription, ...]:
        """Return webhook subscriptions limited to one tenant."""
        return tuple(
            subscription
            for subscription in self.webhook_subscriptions.values()
            if subscription.tenant_account_id == tenant_account_id
        )

    def list_active_webhook_subscriptions(
        self, tenant_account_id: UUID, event_type_code: str
    ) -> tuple[StoredWebhookSubscription, ...]:
        """Return active same-tenant subscriptions that include one event type."""
        return tuple(
            subscription
            for subscription in self.list_webhook_subscriptions(tenant_account_id)
            if subscription.subscription_status == "active"
            and event_type_code in subscription.event_type_set.split(",")
        )

    def revoke_webhook_subscription(
        self, webhook_subscription_id: UUID, revoked_at: datetime
    ) -> StoredWebhookSubscription:
        """Mark one stored subscription revoked.  A second revoke is idempotent."""
        stored = self.webhook_subscriptions.get(webhook_subscription_id)
        if stored is None:
            raise ValueError("webhook subscription revocation requires a stored subscription")
        if stored.subscription_status == "revoked":
            return stored
        updated = StoredWebhookSubscription(
            webhook_subscription_id=stored.webhook_subscription_id,
            tenant_account_id=stored.tenant_account_id,
            webhook_subscription_contract_version=stored.webhook_subscription_contract_version,
            callback_url=stored.callback_url,
            event_type_set=stored.event_type_set,
            webhook_secret_prefix=stored.webhook_secret_prefix,
            webhook_secret_hash=stored.webhook_secret_hash,
            subscription_status="revoked",
            issued_at=stored.issued_at,
            revoked_at=revoked_at,
        )
        self.webhook_subscriptions[updated.webhook_subscription_id] = updated
        return updated

    def insert_webhook_outbox_event(
        self, outbox_event: StoredWebhookOutboxEvent
    ) -> StoredWebhookOutboxEvent:
        """Persist one commercial outbox event.  Replay of the same fact is a no-op."""
        if outbox_event.delivery_status not in {"pending", "delivered"}:
            raise ValueError("delivery_status must be pending or delivered")
        if outbox_event.outbox_event_id in self.webhook_outbox_events:
            raise ValueError("outbox_event_id already stored")
        identity = (
            outbox_event.tenant_account_id,
            outbox_event.event_type_code,
            outbox_event.source_id,
            outbox_event.payload_hash,
        )
        if identity in self.webhook_outbox_identity_index:
            raise ValueError("webhook outbox identity already stored")
        self.webhook_outbox_events[outbox_event.outbox_event_id] = outbox_event
        self.webhook_outbox_identity_index[identity] = outbox_event.outbox_event_id
        return outbox_event

    def find_webhook_outbox_event(
        self,
        tenant_account_id: UUID,
        event_type_code: str,
        source_id: UUID,
        payload_hash: str,
    ) -> StoredWebhookOutboxEvent | None:
        """Return the outbox row for one tenant, event type, source, and hash."""
        outbox_event_id = self.webhook_outbox_identity_index.get(
            (tenant_account_id, event_type_code, source_id, payload_hash)
        )
        if outbox_event_id is None:
            return None
        return self.webhook_outbox_events[outbox_event_id]

    def list_pending_webhook_outbox_events(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookOutboxEvent, ...]:
        """Return pending outbox events limited to one tenant."""
        return tuple(
            outbox_event
            for outbox_event in self.webhook_outbox_events.values()
            if outbox_event.tenant_account_id == tenant_account_id
            and outbox_event.delivery_status == "pending"
        )

    def list_webhook_outbox_events_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookOutboxEvent, ...]:
        """Return all commercial outbox events limited to one tenant."""
        return tuple(
            outbox_event
            for outbox_event in self.webhook_outbox_events.values()
            if outbox_event.tenant_account_id == tenant_account_id
        )

    def mark_webhook_outbox_event_delivered(
        self, outbox_event_id: UUID
    ) -> StoredWebhookOutboxEvent:
        """Mark one outbox event delivered.  A second mark is idempotent."""
        stored = self.webhook_outbox_events.get(outbox_event_id)
        if stored is None:
            raise ValueError("outbox delivery requires a stored event")
        if stored.delivery_status == "delivered":
            return stored
        updated = StoredWebhookOutboxEvent(
            outbox_event_id=stored.outbox_event_id,
            tenant_account_id=stored.tenant_account_id,
            event_type_code=stored.event_type_code,
            payload_hash=stored.payload_hash,
            source_id=stored.source_id,
            occurred_at=stored.occurred_at,
            delivery_status="delivered",
            payload_json=stored.payload_json,
            enqueued_at=stored.enqueued_at,
        )
        self.webhook_outbox_events[updated.outbox_event_id] = updated
        return updated

    def insert_webhook_delivery_attempt(
        self, attempt: StoredWebhookDeliveryAttempt
    ) -> StoredWebhookDeliveryAttempt:
        """Append one delivery attempt.  Attempts are never updated."""
        if attempt.attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        self.webhook_delivery_attempts.append(attempt)
        return attempt

    def list_webhook_delivery_attempts(
        self, outbox_event_id: UUID, webhook_subscription_id: UUID | None = None
    ) -> tuple[StoredWebhookDeliveryAttempt, ...]:
        """Return attempts for one outbox event, optionally one subscription."""
        return tuple(
            attempt
            for attempt in self.webhook_delivery_attempts
            if attempt.outbox_event_id == outbox_event_id
            and (
                webhook_subscription_id is None
                or attempt.webhook_subscription_id == webhook_subscription_id
            )
        )

    def get_webhook_delivery_attempt(
        self, delivery_attempt_id: UUID
    ) -> StoredWebhookDeliveryAttempt | None:
        """Return one delivery attempt by internal identifier, if present."""
        for attempt in self.webhook_delivery_attempts:
            if attempt.delivery_attempt_id == delivery_attempt_id:
                return attempt
        return None

    def get_webhook_outbox_event(
        self, outbox_event_id: UUID
    ) -> StoredWebhookOutboxEvent | None:
        """Return one outbox event by internal identifier, if present."""
        return self.webhook_outbox_events.get(outbox_event_id)

    def list_webhook_delivery_attempts_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookDeliveryAttempt, ...]:
        """Return delivery attempts whose outbox event belongs to one tenant."""
        return tuple(
            attempt
            for attempt in self.webhook_delivery_attempts
            if (outbox := self.webhook_outbox_events.get(attempt.outbox_event_id))
            is not None
            and outbox.tenant_account_id == tenant_account_id
        )

    def require_tenant(self, tenant_reference: str) -> TenantAccount:
        """Return the tenant or raise if the catalog does not contain it."""
        tenant = self.tenant_accounts.get(tenant_reference)
        if tenant is None:
            raise KeyError(tenant_reference)
        return tenant

    def resolve_tenant(
        self, tenant_reference: str
    ) -> tuple[TenantAccount | None, RejectionReasonCode | None]:
        """Resolve a tenant URN without raising."""
        tenant = self.tenant_accounts.get(tenant_reference)
        if tenant is None:
            return None, RejectionReasonCode.TENANT_NOT_FOUND
        return tenant, None

    def resolve_billing_account(
        self, tenant: TenantAccount, billing_account_reference: str
    ) -> tuple[BillingAccount | None, RejectionReasonCode | None]:
        """Resolve a billing account that must belong to *tenant*."""
        if not billing_account_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        account = self.billing_accounts.get(billing_account_reference)
        if account is None:
            return None, RejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND
        if account.tenant_account_id != tenant.tenant_account_id:
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        if account.account_status_code != "active":
            return None, RejectionReasonCode.BILLING_ACCOUNT_NOT_ACTIVE
        return account, None

    def resolve_billing_principal(
        self, tenant: TenantAccount, billing_principal_reference: str, occurred_at: datetime
    ) -> tuple[BillingPrincipal | None, RejectionReasonCode | None]:
        """Resolve a principal that must belong to *tenant* and be effective."""
        if not billing_principal_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        principal = self.billing_principals.get(billing_principal_reference)
        if principal is None:
            return None, RejectionReasonCode.BILLING_PRINCIPAL_NOT_FOUND
        if principal.tenant_account_id != tenant.tenant_account_id:
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        if not _is_effective(principal.valid_from, principal.valid_to, occurred_at):
            return None, RejectionReasonCode.PRINCIPAL_NOT_EFFECTIVE
        return principal, None

    def resolve_credential(
        self,
        tenant: TenantAccount,
        credential_reference: str,
        principal: BillingPrincipal,
        account: BillingAccount,
        occurred_at: datetime,
    ) -> tuple[CredentialRecord | None, RejectionReasonCode | None]:
        """Resolve a credential assigned to the same tenant, principal, and account."""
        if not credential_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        credential = self.credential_records.get(credential_reference)
        if credential is None:
            return None, RejectionReasonCode.CREDENTIAL_NOT_FOUND
        if credential.tenant_account_id != tenant.tenant_account_id:
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        assigned = any(
            assignment.tenant_account_id == tenant.tenant_account_id
            and assignment.credential_record_id == credential.credential_record_id
            and assignment.billing_principal_id == principal.billing_principal_id
            and assignment.billing_account_id == account.billing_account_id
            and _is_effective(assignment.valid_from, assignment.valid_to, occurred_at)
            for assignment in self.credential_assignments
        )
        if not assigned:
            return None, RejectionReasonCode.CREDENTIAL_NOT_ASSIGNED
        return credential, None

    def resolve_meter(
        self, meter_code: str, unit_code: str, quality_code: str, occurred_at: datetime
    ) -> tuple[MeterDefinition | None, RejectionReasonCode | None]:
        """Select the highest effective meter version and enforce unit and quality."""
        candidates = [
            definition
            for definition in self.meter_definitions
            if definition.meter_code == meter_code
            and _is_effective(definition.valid_from, definition.valid_to, occurred_at)
        ]
        if not candidates:
            return None, RejectionReasonCode.METER_NOT_FOUND
        definition = max(candidates, key=lambda item: item.meter_version)
        if definition.unit_code != unit_code:
            return None, RejectionReasonCode.METER_UNIT_MISMATCH
        if (definition.meter_definition_id, quality_code) not in self.meter_quality_rules:
            return None, RejectionReasonCode.METER_QUALITY_NOT_ALLOWED
        return definition, None

    def find_by_source_event_key(
        self, tenant_account_id: UUID, source_event_key: str
    ) -> StoredUsageEvent | None:
        """Return the immutable event for a tenant-scoped source key."""
        usage_event_id = self.source_event_index.get((tenant_account_id, source_event_key))
        if usage_event_id is None:
            return None
        return self.usage_events[usage_event_id]

    def find_by_payload_hash(
        self, tenant_account_id: UUID, event_payload_hash: str, event_contract_version: int
    ) -> StoredUsageEvent | None:
        """Return the event identified by tenant, source-payload hash, and version."""
        usage_event_id = self.payload_hash_index.get(
            (tenant_account_id, event_payload_hash, event_contract_version)
        )
        if usage_event_id is None:
            return None
        return self.usage_events[usage_event_id]

    def find_by_producer_event_id(
        self, tenant_account_id: UUID, producer_event_id: UUID
    ) -> StoredUsageEvent | None:
        """Return the event stored for a tenant-scoped producer event identifier."""
        usage_event_id = self.producer_event_index.get((tenant_account_id, producer_event_id))
        if usage_event_id is None:
            return None
        return self.usage_events[usage_event_id]

    def insert_usage_event(self, event: StoredUsageEvent) -> StoredUsageEvent:
        """Append an immutable usage event.  Existing rows are never updated.

        This in-memory ledger is not thread-safe.  Duplicate checks and insert
        are a single-threaded sequence.  A later PostgreSQL adapter should turn
        unique-constraint violations into replay or conflict receipts.
        """
        source_key = (event.tenant_account_id, event.source_event_key)
        hash_key = (
            event.tenant_account_id,
            event.event_payload_hash,
            event.event_contract_version,
        )
        producer_key = (event.tenant_account_id, event.producer_event_id)
        if (
            event.usage_event_id in self.usage_events
            or source_key in self.source_event_index
            or hash_key in self.payload_hash_index
            or producer_key in self.producer_event_index
        ):
            raise ValueError("usage events are immutable and cannot be replaced")
        self.usage_events[event.usage_event_id] = event
        self.source_event_index[source_key] = event.usage_event_id
        self.payload_hash_index[hash_key] = event.usage_event_id
        self.producer_event_index[producer_key] = event.usage_event_id
        return event

    def append_ingestion_receipt(self, receipt: StoredIngestionReceipt) -> StoredIngestionReceipt:
        """Append an immutable ingest-attempt receipt.  Receipts are never updated."""
        self.usage_ingestion_receipts.append(receipt)
        return receipt

    def list_ingestion_receipts(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredIngestionReceipt, ...]:
        """Return receipts, optionally limited to one tenant."""
        if tenant_account_id is None:
            return tuple(self.usage_ingestion_receipts)
        return tuple(
            receipt
            for receipt in self.usage_ingestion_receipts
            if receipt.tenant_account_id == tenant_account_id
        )

    def get_usage_event(self, usage_event_id: UUID) -> StoredUsageEvent | None:
        """Return one stored usage event by internal identifier, if present."""
        return self.usage_events.get(usage_event_id)

    def list_usage_events(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredUsageEvent, ...]:
        """Return stored usage events, optionally filtered by tenant."""
        events = tuple(self.usage_events.values())
        if tenant_account_id is None:
            return events
        return tuple(
            event for event in events if event.tenant_account_id == tenant_account_id
        )

    def list_usage_events_in_window(
        self, tenant_account_id: UUID, window_started_at: datetime, window_ended_at: datetime
    ) -> tuple[StoredUsageEvent, ...]:
        """Return tenant-scoped events whose ``occurred_at`` is in ``[start, end)``."""
        matched = [
            event
            for event in self.usage_events.values()
            if event.tenant_account_id == tenant_account_id
            and window_started_at <= event.occurred_at < window_ended_at
        ]
        return tuple(sorted(matched, key=lambda event: (event.occurred_at, event.source_event_key)))

    def stored_usage_set(self, tenant_account_id: UUID) -> frozenset[tuple[object, ...]]:
        """Return a deterministic identity set of stored usage for one tenant."""
        identities = []
        for event in self.usage_events.values():
            if event.tenant_account_id != tenant_account_id:
                continue
            measurement_identities = tuple(
                (
                    measurement.meter_code,
                    measurement.measured_quantity,
                    measurement.unit_code,
                    measurement.quality_code,
                )
                for measurement in event.measurements
            )
            identities.append(
                (
                    event.usage_event_id,
                    event.source_event_key,
                    event.event_contract_version,
                    event.event_payload_hash,
                    event.occurred_at,
                    measurement_identities,
                )
            )
        return frozenset(identities)


def _single_urn_segment(urn: str) -> str:
    """Return the single CWL URN segment after ``urn:cwl:``."""
    prefix = "urn:cwl:"
    if not urn.startswith(prefix):
        raise ValueError(f"reference must be a CWL URN: {urn}")
    remainder = urn[len(prefix) :]
    if not remainder or ":" in remainder:
        raise ValueError(f"tenant reference must be a single URN segment: {urn}")
    return remainder


def _require_tenant_scoped_reference(tenant_reference: str, resource_reference: str) -> None:
    """Reject a resource URN that is not prefixed by its tenant URN."""
    if not resource_reference.startswith(f"{tenant_reference}:"):
        raise ValueError("resource reference must stay inside its tenant URN")


def _resource_code(resource_reference: str) -> str:
    """Return the final URN segment used as a stable catalog code."""
    return resource_reference.rsplit(":", 1)[-1]
