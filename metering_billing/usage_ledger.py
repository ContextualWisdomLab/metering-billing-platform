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
