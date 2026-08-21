"""PostgreSQL repository for the durable usage-ingestion vertical slice.

The repository owns the catalog rows, immutable usage facts, measurements, and
ingestion receipts used by :class:`UsageIngestionService`.  Every public
operation uses the supplied PostgreSQL connection; the implementation never
falls back to an in-memory copy.  The broader invoice, collection, provider,
and outbox repositories remain subsequent slices of the persistence port.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import UUID

from metering_billing.errors import (
    RejectionReasonCode,
    UsageEventConflict,
)
from metering_billing.usage_ledger import (
    BillingAccount,
    BillingPrincipal,
    CredentialAssignment,
    CredentialRecord,
    MeterDefinition,
    MeterQualityRule,
    StoredIngestionReceipt,
    StoredUsageEvent,
    StoredUsageMeasurement,
    TenantAccount,
    _require_tenant_scoped_reference,
    _resource_code,
    _single_urn_segment,
    generate_record_id,
)


class PostgresUsageLedger:
    """Persist usage attribution and immutable facts in PostgreSQL.

    ``connection`` is a psycopg 3 connection.  It is injected so callers can
    control pooling and lifecycle; :meth:`connect` is the small convenience
    entry point for a standalone process.  The connection is not closed by
    :meth:`close` unless this repository created it.
    """

    def __init__(self, connection: Any, *, owns_connection: bool = False) -> None:
        self.connection = connection
        self._owns_connection = owns_connection
        self._transaction_active = False

    @classmethod
    def connect(cls, dsn: str) -> "PostgresUsageLedger":
        """Open a psycopg connection for the current migration set."""
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - exercised by packaging smoke
            raise RuntimeError(
                "PostgreSQL support requires the project dependency psycopg[binary]"
            ) from error
        return cls(psycopg.connect(dsn), owns_connection=True)

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """Yield a cursor in the caller transaction or a one-operation transaction."""
        if self._transaction_active:
            with self.connection.cursor() as cursor:
                yield cursor
            return
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                yield cursor

    @contextmanager
    def ingestion_transaction(self) -> Iterator[None]:
        """Commit one ingest decision and its audit receipt atomically."""
        if self._transaction_active:
            yield
            return
        self._transaction_active = True
        try:
            with self.connection.transaction():
                yield
        finally:
            self._transaction_active = False

    def close(self) -> None:
        """Close the connection when this repository owns it."""
        if self._owns_connection:
            self.connection.close()

    def register_tenant(self, tenant_reference: str) -> TenantAccount:
        """Insert or return one tenant authority row."""
        tenant_code = _single_urn_segment(tenant_reference)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.tenant_account
                    (tenant_account_id, tenant_account_code, tenant_reference)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_account_code) DO NOTHING
                RETURNING tenant_account_id, tenant_reference, tenant_account_code
                """,
                (generate_record_id(), tenant_code, tenant_reference),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT tenant_account_id, tenant_reference, tenant_account_code
                    FROM billing_core.tenant_account
                    WHERE tenant_account_code = %s
                    """,
                    (tenant_code,),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - a committed unique row cannot disappear here
                raise RuntimeError("tenant insert did not return a row")
            if row[1] != tenant_reference:  # pragma: no cover - code derives from this URN
                raise ValueError("tenant reference cannot move across identities")
            return TenantAccount(UUID(str(row[0])), row[1], row[2])

    def register_billing_account(
        self,
        tenant_reference: str,
        billing_account_reference: str,
        account_status_code: str = "active",
    ) -> BillingAccount:
        """Insert or return one tenant-scoped billing account."""
        tenant = self._require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, billing_account_reference)
        account_code = _resource_code(billing_account_reference)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.billing_account
                    (billing_account_id, tenant_account_id, billing_account_code, account_status_code)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_account_id, billing_account_code) DO NOTHING
                RETURNING billing_account_id, tenant_account_id, billing_account_code, account_status_code
                """,
                (generate_record_id(), tenant.tenant_account_id, account_code, account_status_code),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT billing_account_id, tenant_account_id, billing_account_code, account_status_code
                    FROM billing_core.billing_account
                    WHERE tenant_account_id = %s AND billing_account_code = %s
                    """,
                    (tenant.tenant_account_id, account_code),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("billing account insert did not return a row")
            return BillingAccount(
                UUID(str(row[0])),
                UUID(str(row[1])),
                billing_account_reference,
                row[2],
                row[3],
            )

    def register_billing_principal(
        self,
        tenant_reference: str,
        billing_principal_reference: str,
        principal_kind_code: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> BillingPrincipal:
        """Insert or return one effective-dated billing principal."""
        tenant = self._require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, billing_principal_reference)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.billing_principal
                    (billing_principal_id, tenant_account_id, principal_kind_code,
                     principal_reference, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_account_id, principal_reference, valid_from) DO NOTHING
                RETURNING billing_principal_id, tenant_account_id, principal_kind_code,
                          principal_reference, valid_from, valid_to
                """,
                (
                    generate_record_id(),
                    tenant.tenant_account_id,
                    principal_kind_code,
                    billing_principal_reference,
                    valid_from,
                    valid_to,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT billing_principal_id, tenant_account_id, principal_kind_code,
                           principal_reference, valid_from, valid_to
                    FROM billing_core.billing_principal
                    WHERE tenant_account_id = %s
                      AND principal_reference = %s
                      AND valid_from = %s
                    """,
                    (tenant.tenant_account_id, billing_principal_reference, valid_from),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("billing principal insert did not return a row")
            return self._principal_from_row(row)

    def register_credential_record(
        self,
        tenant_reference: str,
        credential_reference: str,
        credential_kind_code: str,
        credential_fingerprint: str,
    ) -> CredentialRecord:
        """Insert or return one opaque, non-secret credential record."""
        tenant = self._require_tenant(tenant_reference)
        _require_tenant_scoped_reference(tenant_reference, credential_reference)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.credential_record
                    (credential_record_id, tenant_account_id, credential_reference,
                     credential_kind_code, credential_fingerprint, issuer_reference, issued_at)
                VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (tenant_account_id, credential_reference) DO NOTHING
                RETURNING credential_record_id, tenant_account_id, credential_reference,
                          credential_kind_code, credential_fingerprint
                """,
                (
                    generate_record_id(),
                    tenant.tenant_account_id,
                    credential_reference,
                    credential_kind_code,
                    credential_fingerprint,
                    tenant_reference,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT credential_record_id, tenant_account_id, credential_reference,
                           credential_kind_code, credential_fingerprint
                    FROM billing_core.credential_record
                    WHERE tenant_account_id = %s AND credential_reference = %s
                    """,
                    (tenant.tenant_account_id, credential_reference),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("credential insert did not return a row")
            return self._credential_from_row(row)

    def register_credential_assignment(
        self,
        tenant_reference: str,
        credential_reference: str,
        billing_principal_reference: str,
        billing_account_reference: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> CredentialAssignment:
        """Insert one tenant-safe half-open credential assignment."""
        tenant = self._require_tenant(tenant_reference)
        credential = self._require_credential(tenant, credential_reference)
        principal = self._require_principal(tenant, billing_principal_reference)
        account = self._require_account(tenant, billing_account_reference)
        if valid_to is not None and valid_to <= valid_from:
            raise ValueError("credential assignment interval must be positive")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.credential_assignment
                    (credential_assignment_id, tenant_account_id, credential_record_id,
                     billing_principal_id, billing_account_id, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING credential_assignment_id, tenant_account_id, credential_record_id,
                          billing_principal_id, billing_account_id, valid_from, valid_to
                """,
                (
                    generate_record_id(),
                    tenant.tenant_account_id,
                    credential.credential_record_id,
                    principal.billing_principal_id,
                    account.billing_account_id,
                    valid_from,
                    valid_to,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT credential_assignment_id, tenant_account_id, credential_record_id,
                           billing_principal_id, billing_account_id, valid_from, valid_to
                    FROM billing_core.credential_assignment
                    WHERE credential_record_id = %s AND valid_from = %s
                    """,
                    (credential.credential_record_id, valid_from),
                )
                row = cursor.fetchone()
                if row is None:  # pragma: no cover - exclusion constraint protects the row
                    raise ValueError("credential assignment intervals cannot overlap")
            return self._assignment_from_row(row)

    def register_meter_definition(
        self,
        meter_code: str,
        meter_version: int,
        unit_code: str,
        aggregation_code: str,
        valid_from: datetime,
        valid_to: datetime | None = None,
    ) -> MeterDefinition:
        """Insert or return one versioned meter definition."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.meter_definition
                    (meter_definition_id, meter_code, meter_version, unit_code,
                     aggregation_code, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (meter_code, meter_version) DO NOTHING
                RETURNING meter_definition_id, meter_code, meter_version, unit_code,
                          aggregation_code, valid_from, valid_to
                """,
                (
                    generate_record_id(),
                    meter_code,
                    meter_version,
                    unit_code,
                    aggregation_code,
                    valid_from,
                    valid_to,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT meter_definition_id, meter_code, meter_version, unit_code,
                           aggregation_code, valid_from, valid_to
                    FROM billing_core.meter_definition
                    WHERE meter_code = %s AND meter_version = %s
                    """,
                    (meter_code, meter_version),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("meter definition insert did not return a row")
            return self._meter_from_row(row)

    def register_meter_quality_rule(
        self,
        meter_definition_id: UUID,
        quality_code: str,
        billing_disposition_code: str,
    ) -> MeterQualityRule:
        """Insert or return one meter quality disposition."""
        with self._cursor() as cursor:
            rule_id = generate_record_id()
            cursor.execute(
                """
                INSERT INTO billing_core.meter_quality_rule
                    (meter_quality_rule_id, meter_definition_id, quality_code,
                     billing_disposition_code)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (meter_definition_id, quality_code) DO NOTHING
                RETURNING meter_quality_rule_id, meter_definition_id, quality_code,
                          billing_disposition_code
                """,
                (rule_id, meter_definition_id, quality_code, billing_disposition_code),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT meter_quality_rule_id, meter_definition_id, quality_code,
                           billing_disposition_code
                    FROM billing_core.meter_quality_rule
                    WHERE meter_definition_id = %s AND quality_code = %s
                    """,
                    (meter_definition_id, quality_code),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("meter quality rule insert did not return a row")
            return MeterQualityRule(UUID(str(row[0])), UUID(str(row[1])), row[2], row[3])

    def resolve_tenant(
        self, tenant_reference: str
    ) -> tuple[TenantAccount | None, RejectionReasonCode | None]:
        """Resolve one tenant without exposing another tenant's catalog."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_account_id, tenant_reference, tenant_account_code
                FROM billing_core.tenant_account
                WHERE tenant_reference = %s
                """,
                (tenant_reference,),
            )
            row = cursor.fetchone()
        if row is None:
            return None, RejectionReasonCode.TENANT_NOT_FOUND
        return TenantAccount(UUID(str(row[0])), row[1], row[2]), None

    def resolve_billing_account(
        self, tenant: TenantAccount, billing_account_reference: str
    ) -> tuple[BillingAccount | None, RejectionReasonCode | None]:
        """Resolve an active account by composite tenant identity."""
        if not billing_account_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT billing_account_id, tenant_account_id, billing_account_code,
                       account_status_code
                FROM billing_core.billing_account
                WHERE tenant_account_id = %s AND billing_account_code = %s
                """,
                (tenant.tenant_account_id, _resource_code(billing_account_reference)),
            )
            row = cursor.fetchone()
        if row is None:
            return None, RejectionReasonCode.BILLING_ACCOUNT_NOT_FOUND
        account = BillingAccount(
            UUID(str(row[0])), UUID(str(row[1])), billing_account_reference, row[2], row[3]
        )
        if account.account_status_code != "active":
            return None, RejectionReasonCode.BILLING_ACCOUNT_NOT_ACTIVE
        return account, None

    def resolve_billing_principal(
        self, tenant: TenantAccount, billing_principal_reference: str, occurred_at: datetime
    ) -> tuple[BillingPrincipal | None, RejectionReasonCode | None]:
        """Resolve an effective principal using PostgreSQL time predicates."""
        if not billing_principal_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT billing_principal_id, tenant_account_id, principal_kind_code,
                       principal_reference, valid_from, valid_to
                FROM billing_core.billing_principal
                WHERE tenant_account_id = %s
                  AND principal_reference = %s
                  AND valid_from <= %s
                  AND (valid_to IS NULL OR %s < valid_to)
                ORDER BY valid_from DESC
                LIMIT 1
                """,
                (tenant.tenant_account_id, billing_principal_reference, occurred_at, occurred_at),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT 1
                    FROM billing_core.billing_principal
                    WHERE tenant_account_id = %s AND principal_reference = %s
                    LIMIT 1
                    """,
                    (tenant.tenant_account_id, billing_principal_reference),
                )
                if cursor.fetchone() is None:
                    return None, RejectionReasonCode.BILLING_PRINCIPAL_NOT_FOUND
                return None, RejectionReasonCode.PRINCIPAL_NOT_EFFECTIVE
        principal = self._principal_from_row(row)
        return principal, None

    def resolve_credential(
        self,
        tenant: TenantAccount,
        credential_reference: str,
        principal: BillingPrincipal,
        account: BillingAccount,
        occurred_at: datetime,
    ) -> tuple[CredentialRecord | None, RejectionReasonCode | None]:
        """Resolve a credential only when its effective assignment matches both owners."""
        if not credential_reference.startswith(f"{tenant.tenant_reference}:"):
            return None, RejectionReasonCode.ATTRIBUTION_TENANT_MISMATCH
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credential_record_id, tenant_account_id, credential_reference,
                       credential_kind_code, credential_fingerprint
                FROM billing_core.credential_record
                WHERE tenant_account_id = %s AND credential_reference = %s
                """,
                (tenant.tenant_account_id, credential_reference),
            )
            row = cursor.fetchone()
            if row is None:
                return None, RejectionReasonCode.CREDENTIAL_NOT_FOUND
            credential = self._credential_from_row(row)
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.credential_assignment
                WHERE tenant_account_id = %s
                  AND credential_record_id = %s
                  AND billing_principal_id = %s
                  AND billing_account_id = %s
                  AND valid_from <= %s
                  AND (valid_to IS NULL OR %s < valid_to)
                LIMIT 1
                """,
                (
                    tenant.tenant_account_id,
                    credential.credential_record_id,
                    principal.billing_principal_id,
                    account.billing_account_id,
                    occurred_at,
                    occurred_at,
                ),
            )
            if cursor.fetchone() is None:
                return None, RejectionReasonCode.CREDENTIAL_NOT_ASSIGNED
        return credential, None

    def resolve_meter(
        self, meter_code: str, unit_code: str, quality_code: str, occurred_at: datetime
    ) -> tuple[MeterDefinition | None, RejectionReasonCode | None]:
        """Resolve the highest effective meter version and its quality rule."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT meter_definition_id, meter_code, meter_version, unit_code,
                       aggregation_code, valid_from, valid_to
                FROM billing_core.meter_definition
                WHERE meter_code = %s
                  AND valid_from <= %s
                  AND (valid_to IS NULL OR %s < valid_to)
                ORDER BY meter_version DESC
                LIMIT 1
                """,
                (meter_code, occurred_at, occurred_at),
            )
            row = cursor.fetchone()
            if row is None:
                return None, RejectionReasonCode.METER_NOT_FOUND
            meter = self._meter_from_row(row)
            if meter.unit_code != unit_code:
                return None, RejectionReasonCode.METER_UNIT_MISMATCH
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.meter_quality_rule
                WHERE meter_definition_id = %s AND quality_code = %s
                """,
                (meter.meter_definition_id, quality_code),
            )
            if cursor.fetchone() is None:
                return None, RejectionReasonCode.METER_QUALITY_NOT_ALLOWED
        return meter, None

    def find_by_source_event_key(
        self, tenant_account_id: UUID, source_event_key: str
    ) -> StoredUsageEvent | None:
        """Find one immutable event by tenant-scoped source key."""
        return self._find_event(
            """
            SELECT usage_event_id
            FROM billing_core.usage_event
            WHERE tenant_account_id = %s AND source_event_key = %s
            LIMIT 1
            """,
            (tenant_account_id, source_event_key),
        )

    def find_by_payload_hash(
        self, tenant_account_id: UUID, event_payload_hash: str, event_contract_version: int
    ) -> StoredUsageEvent | None:
        """Find one immutable event by tenant, hash, and contract version."""
        return self._find_event(
            """
            SELECT usage_event_id
            FROM billing_core.usage_event
            WHERE tenant_account_id = %s
              AND event_payload_hash = %s
              AND event_contract_version = %s
            LIMIT 1
            """,
            (tenant_account_id, event_payload_hash, event_contract_version),
        )

    def find_by_producer_event_id(
        self, tenant_account_id: UUID, producer_event_id: UUID
    ) -> StoredUsageEvent | None:
        """Find one immutable event by tenant-scoped producer event ID."""
        return self._find_event(
            """
            SELECT usage_event_id
            FROM billing_core.usage_event
            WHERE tenant_account_id = %s AND producer_event_id = %s
            LIMIT 1
            """,
            (tenant_account_id, producer_event_id),
        )

    def insert_usage_event(self, event: StoredUsageEvent) -> StoredUsageEvent:
        """Insert an event and all measurements atomically under database uniqueness."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.usage_event
                    (usage_event_id, producer_event_id, tenant_account_id,
                     billing_account_id, billing_principal_id, credential_record_id,
                     source_event_key, event_contract_version, event_payload_hash,
                     product_code, operation_code, occurred_at, recorded_at,
                     cost_center_reference, project_reference)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING usage_event_id
                """,
                (
                    event.usage_event_id,
                    event.producer_event_id,
                    event.tenant_account_id,
                    event.billing_account_id,
                    event.billing_principal_id,
                    event.credential_record_id,
                    event.source_event_key,
                    event.event_contract_version,
                    event.event_payload_hash,
                    event.product_code,
                    event.operation_code,
                    event.occurred_at,
                    event.recorded_at,
                    event.cost_center_reference,
                    event.project_reference,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                existing = self._find_event_with_cursor(cursor, event.tenant_account_id, event)
                if existing is None:
                    raise ValueError("usage event conflict has no classified existing row")
                if existing.source_event_key == event.source_event_key:
                    if (
                        existing.event_payload_hash == event.event_payload_hash
                        and existing.event_contract_version == event.event_contract_version
                    ):
                        raise UsageEventConflict(existing, duplicate_replay=True)
                    raise UsageEventConflict(
                        existing,
                        duplicate_replay=False,
                        rejection_reason_code=RejectionReasonCode.SOURCE_EVENT_CONFLICT,
                    )
                if (
                    existing.event_payload_hash == event.event_payload_hash
                    and existing.event_contract_version == event.event_contract_version
                ):
                    raise UsageEventConflict(
                        existing,
                        duplicate_replay=False,
                        rejection_reason_code=RejectionReasonCode.PAYLOAD_HASH_CONFLICT,
                    )
                if existing.producer_event_id == event.producer_event_id:
                    raise UsageEventConflict(
                        existing,
                        duplicate_replay=False,
                        rejection_reason_code=RejectionReasonCode.PRODUCER_EVENT_CONFLICT,
                    )
                raise ValueError(  # pragma: no cover - one of the three identity keys matched
                    "usage event conflict is not tenant-classifiable"
                )
            for measurement in event.measurements:
                cursor.execute(
                    """
                    INSERT INTO billing_core.usage_measurement
                        (usage_measurement_id, usage_event_id, meter_definition_id,
                         measured_quantity, quality_code)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        measurement.usage_measurement_id,
                        event.usage_event_id,
                        measurement.meter_definition_id,
                        measurement.measured_quantity,
                        measurement.quality_code,
                    ),
                )
        return event

    def append_ingestion_receipt(self, receipt: StoredIngestionReceipt) -> StoredIngestionReceipt:
        """Append one audit receipt in the current ingest transaction."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.usage_ingestion_receipt
                    (usage_ingestion_receipt_id, tenant_account_id, usage_event_id,
                     source_event_key, event_contract_version, source_payload_hash,
                     ingestion_outcome_code, rejection_reason_code, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    receipt.usage_ingestion_receipt_id,
                    receipt.tenant_account_id,
                    receipt.usage_event_id,
                    receipt.source_event_key,
                    receipt.event_contract_version,
                    receipt.source_payload_hash,
                    receipt.ingestion_outcome_code,
                    receipt.rejection_reason_code,
                    receipt.recorded_at,
                ),
            )
        return receipt

    def list_ingestion_receipts(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredIngestionReceipt, ...]:
        """Return append-only receipts, optionally filtered by tenant."""
        with self._cursor() as cursor:
            if tenant_account_id is None:
                cursor.execute(
                    """
                    SELECT usage_ingestion_receipt_id, tenant_account_id, usage_event_id,
                           source_event_key, event_contract_version, source_payload_hash,
                           ingestion_outcome_code, rejection_reason_code, recorded_at
                    FROM billing_core.usage_ingestion_receipt
                    ORDER BY recorded_at, usage_ingestion_receipt_id
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT usage_ingestion_receipt_id, tenant_account_id, usage_event_id,
                           source_event_key, event_contract_version, source_payload_hash,
                           ingestion_outcome_code, rejection_reason_code, recorded_at
                    FROM billing_core.usage_ingestion_receipt
                    WHERE tenant_account_id = %s
                    ORDER BY recorded_at, usage_ingestion_receipt_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(self._receipt_from_row(row) for row in cursor.fetchall())

    def get_usage_event(self, usage_event_id: UUID) -> StoredUsageEvent | None:
        """Return one stored usage event by opaque identifier."""
        return self._find_event(
            """
            SELECT usage_event_id
            FROM billing_core.usage_event
            WHERE usage_event_id = %s
            LIMIT 1
            """,
            (usage_event_id,),
        )

    def list_usage_events(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredUsageEvent, ...]:
        """Return immutable events, optionally limited to one tenant."""
        with self._cursor() as cursor:
            if tenant_account_id is None:
                cursor.execute(
                    """
                    SELECT usage_event_id
                    FROM billing_core.usage_event
                    ORDER BY recorded_at, usage_event_id
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT usage_event_id
                    FROM billing_core.usage_event
                    WHERE tenant_account_id = %s
                    ORDER BY recorded_at, usage_event_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(
                self._fetch_usage_event(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def list_usage_events_in_window(
        self, tenant_account_id: UUID, window_started_at: datetime, window_ended_at: datetime
    ) -> tuple[StoredUsageEvent, ...]:
        """Return tenant events in the half-open occurred-at window."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT usage_event_id
                FROM billing_core.usage_event
                WHERE tenant_account_id = %s
                  AND occurred_at >= %s
                  AND occurred_at < %s
                ORDER BY occurred_at, source_event_key
                """,
                (tenant_account_id, window_started_at, window_ended_at),
            )
            return tuple(
                self._fetch_usage_event(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def stored_usage_set(self, tenant_account_id: UUID) -> frozenset[tuple[object, ...]]:
        """Return the same deterministic identity projection as the reference ledger."""
        identities = []
        for event in self.list_usage_events(tenant_account_id):
            identities.append(
                (
                    event.usage_event_id,
                    event.source_event_key,
                    event.event_contract_version,
                    event.event_payload_hash,
                    event.occurred_at,
                    tuple(
                        (
                            measurement.meter_code,
                            measurement.measured_quantity,
                            measurement.unit_code,
                            measurement.quality_code,
                        )
                        for measurement in event.measurements
                    ),
                )
            )
        return frozenset(identities)

    def require_tenant(self, tenant_reference: str) -> TenantAccount:
        """Return a tenant or raise the reference-ledger-compatible KeyError."""
        tenant = self.resolve_tenant(tenant_reference)[0]
        if tenant is None:
            raise KeyError(tenant_reference)
        return tenant

    def _require_tenant(self, tenant_reference: str) -> TenantAccount:
        """Resolve a registration tenant or raise a stable catalog error."""
        return self.require_tenant(tenant_reference)

    def _require_account(self, tenant: TenantAccount, reference: str) -> BillingAccount:
        """Resolve an account for catalog registration."""
        _require_tenant_scoped_reference(tenant.tenant_reference, reference)
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT billing_account_id, tenant_account_id, billing_account_code,
                       account_status_code
                FROM billing_core.billing_account
                WHERE tenant_account_id = %s AND billing_account_code = %s
                """,
                (tenant.tenant_account_id, _resource_code(reference)),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(reference)
        return BillingAccount(
            UUID(str(row[0])), UUID(str(row[1])), reference, row[2], row[3]
        )

    def _require_principal(self, tenant: TenantAccount, reference: str) -> BillingPrincipal:
        """Resolve a principal for catalog registration without an event time."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT billing_principal_id, tenant_account_id, principal_kind_code,
                       principal_reference, valid_from, valid_to
                FROM billing_core.billing_principal
                WHERE tenant_account_id = %s AND principal_reference = %s
                ORDER BY valid_from DESC
                LIMIT 1
                """,
                (tenant.tenant_account_id, reference),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(reference)
        return self._principal_from_row(row)

    def _require_credential(self, tenant: TenantAccount, reference: str) -> CredentialRecord:
        """Resolve a credential for catalog registration."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credential_record_id, tenant_account_id, credential_reference,
                       credential_kind_code, credential_fingerprint
                FROM billing_core.credential_record
                WHERE tenant_account_id = %s AND credential_reference = %s
                """,
                (tenant.tenant_account_id, reference),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(reference)
        return self._credential_from_row(row)

    def _find_event(self, query: str, parameters: tuple[Any, ...]) -> StoredUsageEvent | None:
        """Run one fixed identity query and hydrate its measurements."""
        with self._cursor() as cursor:
            cursor.execute(query, parameters)
            row = cursor.fetchone()
            if row is None:
                return None
            return self._fetch_usage_event(cursor, UUID(str(row[0])))

    def _find_event_with_cursor(
        self, cursor: Any, tenant_account_id: UUID, event: StoredUsageEvent
    ) -> StoredUsageEvent | None:
        """Classify a unique conflict by querying each tenant-scoped event identity."""
        for predicate, parameters in (
            (
                """
                SELECT usage_event_id
                FROM billing_core.usage_event
                WHERE tenant_account_id = %s AND source_event_key = %s
                LIMIT 1
                """,
                (tenant_account_id, event.source_event_key),
            ),
            (
                """
                SELECT usage_event_id
                FROM billing_core.usage_event
                WHERE tenant_account_id = %s
                  AND event_payload_hash = %s
                  AND event_contract_version = %s
                LIMIT 1
                """,
                (tenant_account_id, event.event_payload_hash, event.event_contract_version),
            ),
            (
                """
                SELECT usage_event_id
                FROM billing_core.usage_event
                WHERE tenant_account_id = %s AND producer_event_id = %s
                LIMIT 1
                """,
                (tenant_account_id, event.producer_event_id),
            ),
        ):
            cursor.execute(predicate, parameters)
            row = cursor.fetchone()
            if row is not None:
                return self._fetch_usage_event(cursor, UUID(str(row[0])))
        return None

    def _fetch_usage_event(self, cursor: Any, usage_event_id: UUID) -> StoredUsageEvent:
        """Hydrate one event and its normalized measurement rows on one cursor."""
        cursor.execute(
            """
            SELECT usage_event_id, producer_event_id, tenant_account_id, billing_account_id,
                   billing_principal_id, credential_record_id, source_event_key,
                   event_contract_version, event_payload_hash, product_code, operation_code,
                   occurred_at, recorded_at, cost_center_reference, project_reference
            FROM billing_core.usage_event
            WHERE usage_event_id = %s
            """,
            (usage_event_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - the caller selected an existing row
            raise KeyError(usage_event_id)
        cursor.execute(
            """
            SELECT usage_measurement_id, usage_event_id, meter_definition_id,
                   meter_code, unit_code, measured_quantity, quality_code
            FROM billing_core.usage_measurement
            JOIN billing_core.meter_definition USING (meter_definition_id)
            WHERE usage_event_id = %s
            ORDER BY usage_measurement_id
            """,
            (usage_event_id,),
        )
        measurements = tuple(self._measurement_from_row(measurement) for measurement in cursor.fetchall())
        return StoredUsageEvent(
            usage_event_id=UUID(str(row[0])),
            producer_event_id=UUID(str(row[1])),
            tenant_account_id=UUID(str(row[2])),
            billing_account_id=UUID(str(row[3])),
            billing_principal_id=UUID(str(row[4])),
            credential_record_id=None if row[5] is None else UUID(str(row[5])),
            source_event_key=row[6],
            event_contract_version=row[7],
            event_payload_hash=row[8],
            product_code=row[9],
            operation_code=row[10],
            occurred_at=row[11],
            recorded_at=row[12],
            cost_center_reference=row[13],
            project_reference=row[14],
            measurements=measurements,
        )

    @staticmethod
    def _principal_from_row(row: tuple[Any, ...]) -> BillingPrincipal:
        """Decode a principal query row."""
        return BillingPrincipal(
            UUID(str(row[0])),
            UUID(str(row[1])),
            row[3],
            row[2],
            row[3],
            row[4],
            row[5],
        )

    @staticmethod
    def _credential_from_row(row: tuple[Any, ...]) -> CredentialRecord:
        """Decode a credential query row without exposing any secret."""
        return CredentialRecord(UUID(str(row[0])), UUID(str(row[1])), row[2], row[3], row[4])

    @staticmethod
    def _assignment_from_row(row: tuple[Any, ...]) -> CredentialAssignment:
        """Decode an assignment query row."""
        return CredentialAssignment(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            row[5],
            row[6],
        )

    @staticmethod
    def _meter_from_row(row: tuple[Any, ...]) -> MeterDefinition:
        """Decode a meter query row."""
        return MeterDefinition(
            UUID(str(row[0])), row[1], row[2], row[3], row[4], row[5], row[6]
        )

    @staticmethod
    def _measurement_from_row(row: tuple[Any, ...]) -> StoredUsageMeasurement:
        """Decode a normalized measurement join row."""
        return StoredUsageMeasurement(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
        )

    @staticmethod
    def _receipt_from_row(row: tuple[Any, ...]) -> StoredIngestionReceipt:
        """Decode an append-only receipt row."""
        return StoredIngestionReceipt(
            UUID(str(row[0])),
            None if row[1] is None else UUID(str(row[1])),
            None if row[2] is None else UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
        )
