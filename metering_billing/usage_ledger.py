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

from metering_billing.errors import RatingRejectionReasonCode, RejectionReasonCode
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal


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
class RateCard:
    """Versioned commercial price book used to rate a usage window."""

    rate_card_id: UUID
    rate_card_code: str
    rate_card_version: int
    currency_code: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class RateCardPrice:
    """Exact unit price for one meter on one rate-card version."""

    rate_card_price_id: UUID
    rate_card_id: UUID
    meter_definition_id: UUID
    unit_price_amount: Decimal


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
    """Append-only balanced journal proposal for one tenant invoice draft."""

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
    """Append-only commercial collection case for one tenant invoice draft."""

    collection_case_id: UUID
    tenant_account_id: UUID
    invoice_draft_id: UUID
    currency_code: str
    collection_case_status: str
    outstanding_amount: Decimal
    opened_at: datetime


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
    rate_cards: dict[tuple[str, int], RateCard] = field(default_factory=dict)
    rate_card_prices: dict[tuple[UUID, UUID], RateCardPrice] = field(default_factory=dict)
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
    journal_proposal_lines: list[StoredJournalProposalLine] = field(default_factory=list)
    collection_cases: dict[UUID, StoredCollectionCase] = field(default_factory=dict)
    collection_case_index: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    collection_dunning_events: list[StoredCollectionDunningEvent] = field(default_factory=list)
    collection_dunning_notice_index: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    collection_dunning_number_index: dict[tuple[UUID, int], UUID] = field(default_factory=dict)

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

    def register_rate_card(
        self,
        rate_card_code: str,
        rate_card_version: int,
        currency_code: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> RateCard:
        """Register a versioned rate card.  The same code and version is idempotent."""
        key = (rate_card_code, rate_card_version)
        existing = self.rate_cards.get(key)
        if existing is not None:
            return existing
        rate_card = RateCard(
            rate_card_id=generate_record_id(),
            rate_card_code=rate_card_code,
            rate_card_version=rate_card_version,
            currency_code=currency_code,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.rate_cards[key] = rate_card
        return rate_card

    def register_rate_card_price(
        self,
        rate_card_id: UUID,
        meter_definition_id: UUID,
        unit_price_amount: str,
    ) -> RateCardPrice:
        """Register an exact unit price.  Binary floating-point values are rejected."""
        parsed_amount = parse_exact_decimal(unit_price_amount)
        key = (rate_card_id, meter_definition_id)
        existing = self.rate_card_prices.get(key)
        if existing is not None:
            return existing
        price = RateCardPrice(
            rate_card_price_id=generate_record_id(),
            rate_card_id=rate_card_id,
            meter_definition_id=meter_definition_id,
            unit_price_amount=parsed_amount,
        )
        self.rate_card_prices[key] = price
        return price

    def resolve_rate_card(
        self, rate_card_code: str, rate_card_version: int, occurred_at: datetime
    ) -> tuple[RateCard | None, RatingRejectionReasonCode | None]:
        """Resolve one rate-card version and require it to be effective at *occurred_at*."""
        rate_card = self.rate_cards.get((rate_card_code, rate_card_version))
        if rate_card is None:
            return None, RatingRejectionReasonCode.RATE_CARD_NOT_FOUND
        if not _is_effective(rate_card.valid_from, rate_card.valid_to, occurred_at):
            return None, RatingRejectionReasonCode.RATE_CARD_NOT_EFFECTIVE
        return rate_card, None

    def find_rate_card_price(
        self, rate_card_id: UUID, meter_definition_id: UUID
    ) -> RateCardPrice | None:
        """Return the unit price for one meter on one rate-card version."""
        return self.rate_card_prices.get((rate_card_id, meter_definition_id))

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
        if journal_proposal.journal_proposal_id in self.journal_proposals:
            raise ValueError("journal proposals are immutable and cannot be replaced")
        if identity_key in self.journal_proposal_index:
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
        )
        self.journal_proposals[persisted.journal_proposal_id] = persisted
        self.journal_proposal_index[identity_key] = persisted.journal_proposal_id
        self.journal_proposal_lines.extend(parsed_lines)
        return persisted

    def list_journal_proposals(self, tenant_account_id: UUID) -> tuple[StoredJournalProposal, ...]:
        """Return journal proposals limited to one tenant."""
        return tuple(
            proposal
            for proposal in self.journal_proposals.values()
            if proposal.tenant_account_id == tenant_account_id
        )

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

    def list_rating_runs(self, tenant_account_id: UUID) -> tuple[StoredRatingRun, ...]:
        """Return rating runs limited to one tenant."""
        return tuple(
            rating_run
            for rating_run in self.rating_runs.values()
            if rating_run.tenant_account_id == tenant_account_id
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
