"""PostgreSQL repository for the durable usage-to-invoice vertical slice.

The repository owns catalog rows, immutable usage facts, rating runs, invoice
drafts, issued invoices, and the atomic webhook outbox used by the first
commercial path. Every public operation uses the supplied PostgreSQL
connection; the implementation never falls back to an in-memory copy. The
broader collection, provider, and settlement repositories remain subsequent
slices of the persistence port.
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
    StoredInvoiceDraft,
    StoredInvoiceDraftLine,
    StoredIngestionReceipt,
    StoredIssuedInvoice,
    StoredIssuedInvoiceLine,
    StoredRateCard,
    StoredRateCardLine,
    StoredRateCardVersion,
    StoredRatingLine,
    StoredRatingRun,
    StoredTaxRateSchedule,
    StoredTaxRateVersion,
    StoredTaxAssessment,
    StoredUsageEvent,
    StoredUsageMeasurement,
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit one multi-record commercial command atomically."""
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
                    (billing_account_id, tenant_account_id, billing_account_code,
                     billing_account_reference, account_status_code)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_account_id, billing_account_code) DO NOTHING
                RETURNING billing_account_id, tenant_account_id, billing_account_code,
                          billing_account_reference, account_status_code
                """,
                (
                    generate_record_id(),
                    tenant.tenant_account_id,
                    account_code,
                    billing_account_reference,
                    account_status_code,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT billing_account_id, tenant_account_id, billing_account_code,
                           billing_account_reference, account_status_code
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
                row[3],
                row[2],
                row[4],
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

    def find_rate_card(
        self, tenant_account_id: UUID, rate_card_name: str
    ) -> StoredRateCard | None:
        """Return one tenant-scoped price-book header by name."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_id, tenant_account_id, rate_card_name,
                       currency_code, valid_from
                FROM billing_core.rate_card
                WHERE tenant_account_id = %s AND rate_card_name = %s
                """,
                (tenant_account_id, rate_card_name),
            )
            row = cursor.fetchone()
        return None if row is None else self._rate_card_from_row(row)

    def get_rate_card(self, rate_card_id: UUID) -> StoredRateCard | None:
        """Return one price-book header by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_id, tenant_account_id, rate_card_name,
                       currency_code, valid_from
                FROM billing_core.rate_card
                WHERE rate_card_id = %s
                """,
                (rate_card_id,),
            )
            row = cursor.fetchone()
        return None if row is None else self._rate_card_from_row(row)

    def insert_rate_card(self, rate_card: StoredRateCard) -> StoredRateCard:
        """Persist one tenant price-book header without replacing history."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.rate_card
                    (rate_card_id, rate_card_code, rate_card_version, currency_code,
                     valid_from, tenant_account_id, rate_card_name)
                VALUES (%s, %s, 1, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING rate_card_id, tenant_account_id, rate_card_name,
                          currency_code, valid_from
                """,
                (
                    rate_card.rate_card_id,
                    rate_card.rate_card_name,
                    rate_card.currency_code,
                    rate_card.created_at,
                    rate_card.tenant_account_id,
                    rate_card.rate_card_name,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT rate_card_id, tenant_account_id, rate_card_name,
                           currency_code, valid_from
                    FROM billing_core.rate_card
                    WHERE tenant_account_id = %s AND rate_card_name = %s
                    """,
                    (rate_card.tenant_account_id, rate_card.rate_card_name),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover - unique identity protects the header
                raise RuntimeError("rate-card insert did not return a row")
        stored = self._rate_card_from_row(row)
        if stored.currency_code != rate_card.currency_code:
            raise ValueError("rate_card currency cannot change after publish")
        return stored

    def list_rate_cards(self, tenant_account_id: UUID) -> tuple[StoredRateCard, ...]:
        """Return price-book headers limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_id, tenant_account_id, rate_card_name,
                       currency_code, valid_from
                FROM billing_core.rate_card
                WHERE tenant_account_id = %s
                ORDER BY rate_card_name, rate_card_id
                """,
                (tenant_account_id,),
            )
            return tuple(self._rate_card_from_row(row) for row in cursor.fetchall())

    def find_rate_card_version_by_identity(
        self,
        tenant_account_id: UUID,
        rate_card_id: UUID,
        source_payload_hash: str,
        rate_card_contract_version: int,
    ) -> StoredRateCardVersion | None:
        """Return one published version by its immutable payload identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                       version_number, rate_card_contract_version, currency_code,
                       source_payload_hash, published_at
                FROM billing_core.rate_card_version
                WHERE tenant_account_id = %s
                  AND rate_card_id = %s
                  AND source_payload_hash = %s
                  AND rate_card_contract_version = %s
                """,
                (tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version),
            )
            row = cursor.fetchone()
            return None if row is None else self._rate_card_version_from_cursor(cursor, row)

    def get_rate_card_version(self, rate_card_version_id: UUID) -> StoredRateCardVersion | None:
        """Return one published price-book version by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                       version_number, rate_card_contract_version, currency_code,
                       source_payload_hash, published_at
                FROM billing_core.rate_card_version
                WHERE rate_card_version_id = %s
                """,
                (rate_card_version_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._rate_card_version_from_cursor(cursor, row)

    def find_rate_card_version(
        self,
        tenant_account_id: UUID,
        version_number: int,
        rate_card_name: str | None = None,
    ) -> StoredRateCardVersion | None:
        """Return one tenant-scoped version number when it is unambiguous."""
        with self._cursor() as cursor:
            if rate_card_name is None:
                cursor.execute(
                    """
                    SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                           version_number, rate_card_contract_version, currency_code,
                           source_payload_hash, published_at
                    FROM billing_core.rate_card_version
                    WHERE tenant_account_id = %s AND version_number = %s
                    """,
                    (tenant_account_id, version_number),
                )
            else:
                cursor.execute(
                    """
                    SELECT version.rate_card_version_id, version.tenant_account_id,
                           version.rate_card_id, version.version_number,
                           version.rate_card_contract_version, version.currency_code,
                           version.source_payload_hash, version.published_at
                    FROM billing_core.rate_card_version AS version
                    JOIN billing_core.rate_card AS card
                      ON card.tenant_account_id = version.tenant_account_id
                     AND card.rate_card_id = version.rate_card_id
                    WHERE version.tenant_account_id = %s
                      AND card.rate_card_name = %s
                      AND version.version_number = %s
                    """,
                    (tenant_account_id, rate_card_name, version_number),
                )
            rows = cursor.fetchall()
            if len(rows) != 1:
                return None
            return self._rate_card_version_from_cursor(cursor, rows[0])

    def next_rate_card_version_number(
        self, tenant_account_id: UUID, rate_card_id: UUID
    ) -> int:
        """Return the next append-only version number for one price book."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM billing_core.rate_card_version
                WHERE tenant_account_id = %s AND rate_card_id = %s
                """,
                (tenant_account_id, rate_card_id),
            )
            return int(cursor.fetchone()[0])

    def insert_rate_card_version(
        self, version: StoredRateCardVersion
    ) -> StoredRateCardVersion:
        """Persist one immutable price-book version and its normalized lines."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.rate_card_version
                    (rate_card_version_id, tenant_account_id, rate_card_id,
                     version_number, rate_card_contract_version, currency_code,
                     source_payload_hash, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING rate_card_version_id, tenant_account_id, rate_card_id,
                          version_number, rate_card_contract_version, currency_code,
                          source_payload_hash, published_at
                """,
                (
                    version.rate_card_version_id,
                    version.tenant_account_id,
                    version.rate_card_id,
                    version.version_number,
                    version.rate_card_contract_version,
                    version.currency_code,
                    version.source_payload_hash,
                    version.published_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                           version_number, rate_card_contract_version, currency_code,
                           source_payload_hash, published_at
                    FROM billing_core.rate_card_version
                    WHERE tenant_account_id = %s
                      AND rate_card_id = %s
                      AND source_payload_hash = %s
                      AND rate_card_contract_version = %s
                    """,
                    (
                        version.tenant_account_id,
                        version.rate_card_id,
                        version.source_payload_hash,
                        version.rate_card_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:  # pragma: no cover - unique identity protects the version
                    raise RuntimeError("rate-card version insert did not return a row")
                return self._rate_card_version_from_cursor(cursor, row)
            for line in version.rate_card_lines:
                cursor.execute(
                    """
                    INSERT INTO billing_core.rate_card_line
                        (rate_card_line_id, tenant_account_id, rate_card_version_id,
                         metric_code, unit_amount, currency_code)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        line.rate_card_line_id,
                        line.tenant_account_id,
                        line.rate_card_version_id,
                        line.metric_code,
                        line.unit_amount,
                        line.currency_code,
                    ),
                )
            return self._rate_card_version_from_cursor(cursor, row)

    def list_rate_card_versions(
        self, tenant_account_id: UUID, rate_card_id: UUID | None = None
    ) -> tuple[StoredRateCardVersion, ...]:
        """Return published price-book versions for one tenant."""
        with self._cursor() as cursor:
            if rate_card_id is None:
                cursor.execute(
                    """
                    SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                           version_number, rate_card_contract_version, currency_code,
                           source_payload_hash, published_at
                    FROM billing_core.rate_card_version
                    WHERE tenant_account_id = %s
                    ORDER BY rate_card_id, version_number
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT rate_card_version_id, tenant_account_id, rate_card_id,
                           version_number, rate_card_contract_version, currency_code,
                           source_payload_hash, published_at
                    FROM billing_core.rate_card_version
                    WHERE tenant_account_id = %s AND rate_card_id = %s
                    ORDER BY version_number
                    """,
                    (tenant_account_id, rate_card_id),
                )
            return tuple(
                self._rate_card_version_from_cursor(cursor, row)
                for row in cursor.fetchall()
            )

    def find_rate_card_line(
        self, rate_card_version_id: UUID, metric_code: str
    ) -> StoredRateCardLine | None:
        """Return one exact unit price from a published version."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_card_line_id, tenant_account_id, rate_card_version_id,
                       metric_code, unit_amount, currency_code
                FROM billing_core.rate_card_line
                WHERE rate_card_version_id = %s AND metric_code = %s
                """,
                (rate_card_version_id, metric_code),
            )
            row = cursor.fetchone()
        return None if row is None else self._rate_card_line_from_row(row)

    def find_meter_quality_rule(
        self, meter_definition_id: UUID, quality_code: str
    ) -> MeterQualityRule | None:
        """Return the billing disposition for one normalized meter quality."""
        with self._cursor() as cursor:
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
        if row is None:
            return None
        return MeterQualityRule(UUID(str(row[0])), UUID(str(row[1])), row[2], row[3])

    def find_tax_rate_schedule(
        self, tenant_account_id: UUID, tax_code: str
    ) -> StoredTaxRateSchedule | None:
        """Return one tenant-scoped tax-rate schedule by code."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_rate_schedule_id, tenant_account_id, tax_code, created_at
                FROM billing_core.tax_rate_schedule
                WHERE tenant_account_id = %s AND tax_code = %s
                """,
                (tenant_account_id, tax_code),
            )
            row = cursor.fetchone()
        return None if row is None else self._tax_rate_schedule_from_row(row)

    def get_tax_rate_schedule(
        self, tax_rate_schedule_id: UUID
    ) -> StoredTaxRateSchedule | None:
        """Return one tax-rate schedule by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_rate_schedule_id, tenant_account_id, tax_code, created_at
                FROM billing_core.tax_rate_schedule
                WHERE tax_rate_schedule_id = %s
                """,
                (tax_rate_schedule_id,),
            )
            row = cursor.fetchone()
        return None if row is None else self._tax_rate_schedule_from_row(row)

    def insert_tax_rate_schedule(
        self, schedule: StoredTaxRateSchedule
    ) -> StoredTaxRateSchedule:
        """Persist one tax-rate schedule without replacing its code identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.tax_rate_schedule
                    (tax_rate_schedule_id, tenant_account_id, tax_code, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING tax_rate_schedule_id, tenant_account_id, tax_code, created_at
                """,
                (
                    schedule.tax_rate_schedule_id,
                    schedule.tenant_account_id,
                    schedule.tax_code,
                    schedule.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT tax_rate_schedule_id, tenant_account_id, tax_code, created_at
                    FROM billing_core.tax_rate_schedule
                    WHERE tenant_account_id = %s AND tax_code = %s
                    """,
                    (schedule.tenant_account_id, schedule.tax_code),
                )
                row = cursor.fetchone()
            if row is None:
                raise ValueError("tax-rate schedule identity already belongs to another row")
        return self._tax_rate_schedule_from_row(row)

    def list_tax_rate_schedules(
        self, tenant_account_id: UUID
    ) -> tuple[StoredTaxRateSchedule, ...]:
        """Return tax-rate schedules limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_rate_schedule_id, tenant_account_id, tax_code, created_at
                FROM billing_core.tax_rate_schedule
                WHERE tenant_account_id = %s
                ORDER BY tax_code, tax_rate_schedule_id
                """,
                (tenant_account_id,),
            )
            return tuple(self._tax_rate_schedule_from_row(row) for row in cursor.fetchall())

    def find_tax_rate_version_by_identity(
        self,
        tenant_account_id: UUID,
        tax_rate_schedule_id: UUID,
        source_payload_hash: str,
        tax_rate_contract_version: int,
    ) -> StoredTaxRateVersion | None:
        """Return one published tax-rate version by immutable payload identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                       version_number, tax_rate_contract_version, tax_code, tax_rate,
                       source_payload_hash, published_at
                FROM billing_core.tax_rate_version
                WHERE tenant_account_id = %s
                  AND tax_rate_schedule_id = %s
                  AND source_payload_hash = %s
                  AND tax_rate_contract_version = %s
                """,
                (
                    tenant_account_id,
                    tax_rate_schedule_id,
                    source_payload_hash,
                    tax_rate_contract_version,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else self._tax_rate_version_from_row(row)

    def get_tax_rate_version(
        self, tax_rate_version_id: UUID
    ) -> StoredTaxRateVersion | None:
        """Return one published tax-rate version by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                       version_number, tax_rate_contract_version, tax_code, tax_rate,
                       source_payload_hash, published_at
                FROM billing_core.tax_rate_version
                WHERE tax_rate_version_id = %s
                """,
                (tax_rate_version_id,),
            )
            row = cursor.fetchone()
        return None if row is None else self._tax_rate_version_from_row(row)

    def find_tax_rate_version(
        self,
        tenant_account_id: UUID,
        version_number: int,
        tax_code: str | None = None,
    ) -> StoredTaxRateVersion | None:
        """Return one tenant-scoped version number when it is unambiguous."""
        with self._cursor() as cursor:
            if tax_code is None:
                cursor.execute(
                    """
                    SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                           version_number, tax_rate_contract_version, tax_code, tax_rate,
                           source_payload_hash, published_at
                    FROM billing_core.tax_rate_version
                    WHERE tenant_account_id = %s AND version_number = %s
                    """,
                    (tenant_account_id, version_number),
                )
            else:
                cursor.execute(
                    """
                    SELECT version.tax_rate_version_id, version.tenant_account_id,
                           version.tax_rate_schedule_id, version.version_number,
                           version.tax_rate_contract_version, version.tax_code,
                           version.tax_rate, version.source_payload_hash,
                           version.published_at
                    FROM billing_core.tax_rate_version AS version
                    JOIN billing_core.tax_rate_schedule AS schedule
                      ON schedule.tenant_account_id = version.tenant_account_id
                     AND schedule.tax_rate_schedule_id = version.tax_rate_schedule_id
                    WHERE version.tenant_account_id = %s
                      AND schedule.tax_code = %s
                      AND version.version_number = %s
                    """,
                    (tenant_account_id, tax_code, version_number),
                )
            rows = cursor.fetchall()
        if len(rows) != 1:
            return None
        return self._tax_rate_version_from_row(rows[0])

    def next_tax_rate_version_number(
        self, tenant_account_id: UUID, tax_rate_schedule_id: UUID
    ) -> int:
        """Return the next append-only version number for one tax schedule."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM billing_core.tax_rate_version
                WHERE tenant_account_id = %s AND tax_rate_schedule_id = %s
                """,
                (tenant_account_id, tax_rate_schedule_id),
            )
            return int(cursor.fetchone()[0])

    def insert_tax_rate_version(
        self, version: StoredTaxRateVersion
    ) -> StoredTaxRateVersion:
        """Persist one immutable tax-rate version and classify replay."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.tax_rate_version
                    (tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                     version_number, tax_rate_contract_version, tax_code, tax_rate,
                     source_payload_hash, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING tax_rate_version_id, tenant_account_id,
                          tax_rate_schedule_id, version_number,
                          tax_rate_contract_version, tax_code, tax_rate,
                          source_payload_hash, published_at
                """,
                (
                    version.tax_rate_version_id,
                    version.tenant_account_id,
                    version.tax_rate_schedule_id,
                    version.version_number,
                    version.tax_rate_contract_version,
                    version.tax_code,
                    version.tax_rate,
                    version.source_payload_hash,
                    version.published_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                           version_number, tax_rate_contract_version, tax_code, tax_rate,
                           source_payload_hash, published_at
                    FROM billing_core.tax_rate_version
                    WHERE tenant_account_id = %s
                      AND tax_rate_schedule_id = %s
                      AND source_payload_hash = %s
                      AND tax_rate_contract_version = %s
                    """,
                    (
                        version.tenant_account_id,
                        version.tax_rate_schedule_id,
                        version.source_payload_hash,
                        version.tax_rate_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("tax-rate version identity already belongs to another row")
            return self._tax_rate_version_from_row(row)

    def list_tax_rate_versions(
        self, tenant_account_id: UUID, tax_rate_schedule_id: UUID | None = None
    ) -> tuple[StoredTaxRateVersion, ...]:
        """Return published tax-rate versions limited to one tenant."""
        with self._cursor() as cursor:
            if tax_rate_schedule_id is None:
                cursor.execute(
                    """
                    SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                           version_number, tax_rate_contract_version, tax_code, tax_rate,
                           source_payload_hash, published_at
                    FROM billing_core.tax_rate_version
                    WHERE tenant_account_id = %s
                    ORDER BY tax_rate_schedule_id, version_number
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT tax_rate_version_id, tenant_account_id, tax_rate_schedule_id,
                           version_number, tax_rate_contract_version, tax_code, tax_rate,
                           source_payload_hash, published_at
                    FROM billing_core.tax_rate_version
                    WHERE tenant_account_id = %s AND tax_rate_schedule_id = %s
                    ORDER BY version_number
                    """,
                    (tenant_account_id, tax_rate_schedule_id),
                )
            return tuple(self._tax_rate_version_from_row(row) for row in cursor.fetchall())

    def find_tax_assessment(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        tax_rate_version_id: UUID,
        source_payload_hash: str,
        tax_assessment_contract_version: int,
    ) -> StoredTaxAssessment | None:
        """Return one assessment by its tenant-scoped immutable identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT assessment.tax_assessment_id
                FROM billing_core.tax_assessment AS assessment
                WHERE assessment.tenant_account_id = %s
                  AND assessment.invoice_draft_id = %s
                  AND assessment.tax_rate_version_id = %s
                  AND assessment.source_payload_hash = %s
                  AND assessment.tax_assessment_contract_version = %s
                """,
                (
                    tenant_account_id,
                    invoice_draft_id,
                    tax_rate_version_id,
                    source_payload_hash,
                    tax_assessment_contract_version,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_tax_assessment(cursor, UUID(str(row[0])))

    def get_tax_assessment(
        self, tax_assessment_id: UUID
    ) -> StoredTaxAssessment | None:
        """Return one tax assessment by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tax_assessment_id
                FROM billing_core.tax_assessment
                WHERE tax_assessment_id = %s
                """,
                (tax_assessment_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_tax_assessment(cursor, UUID(str(row[0])))

    def list_tax_assessments(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredTaxAssessment, ...]:
        """Return assessments, optionally limited to one tenant."""
        with self._cursor() as cursor:
            if tenant_account_id is None:
                cursor.execute(
                    """
                    SELECT tax_assessment_id
                    FROM billing_core.tax_assessment
                    ORDER BY assessed_at, tax_assessment_id
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT tax_assessment_id
                    FROM billing_core.tax_assessment
                    WHERE tenant_account_id = %s
                    ORDER BY assessed_at, tax_assessment_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(
                self._fetch_tax_assessment(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_tax_assessment(
        self, assessment: StoredTaxAssessment
    ) -> StoredTaxAssessment:
        """Persist one tax snapshot and classify exact or draft replay."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.tax_assessment
                    (tax_assessment_id, tenant_account_id, invoice_draft_id,
                     tax_rate_version_id, tax_assessment_contract_version, tax_code,
                     tax_rate, currency_code, tax_exclusive_amount, tax_amount,
                     tax_inclusive_amount, source_payload_hash, assessed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING tax_assessment_id
                """,
                (
                    assessment.tax_assessment_id,
                    assessment.tenant_account_id,
                    assessment.invoice_draft_id,
                    assessment.tax_rate_version_id,
                    assessment.tax_assessment_contract_version,
                    assessment.tax_code,
                    assessment.tax_rate,
                    assessment.currency_code,
                    assessment.tax_exclusive_amount,
                    assessment.tax_amount,
                    assessment.tax_inclusive_amount,
                    assessment.source_payload_hash,
                    assessment.assessed_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT tax_assessment_id
                    FROM billing_core.tax_assessment
                    WHERE tenant_account_id = %s
                      AND invoice_draft_id = %s
                      AND tax_rate_version_id = %s
                      AND source_payload_hash = %s
                      AND tax_assessment_contract_version = %s
                    """,
                    (
                        assessment.tenant_account_id,
                        assessment.invoice_draft_id,
                        assessment.tax_rate_version_id,
                        assessment.source_payload_hash,
                        assessment.tax_assessment_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("tax assessment identity already belongs to another row")
                return self._fetch_tax_assessment(cursor, UUID(str(row[0])))
            return self._fetch_tax_assessment(cursor, UUID(str(row[0])))

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

    def billing_account_reference_for(self, billing_account_id: UUID) -> str:
        """Return the tenant-scoped URN for one billing-account identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT account.billing_account_reference
                FROM billing_core.billing_account AS account
                JOIN billing_core.tenant_account AS tenant
                  ON tenant.tenant_account_id = account.tenant_account_id
                WHERE account.billing_account_id = %s
                """,
                (billing_account_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(billing_account_id)
        return row[0]

    def find_rating_run(
        self,
        tenant_account_id: UUID,
        window_started_at: datetime,
        window_ended_at: datetime,
        rate_card_id: UUID,
        usage_snapshot_hash: str,
        rate_card_version: int | None = None,
    ) -> StoredRatingRun | None:
        """Return one immutable rating result by its replay identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rating_run_id
                FROM billing_core.rating_run
                WHERE tenant_account_id = %s
                  AND window_started_at = %s
                  AND window_ended_at = %s
                  AND rate_card_id = %s
                  AND usage_snapshot_hash = %s
                  AND (%s::integer IS NULL OR rate_card_version = %s::integer)
                """,
                (
                    tenant_account_id,
                    window_started_at,
                    window_ended_at,
                    rate_card_id,
                    usage_snapshot_hash,
                    rate_card_version,
                    rate_card_version,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_rating_run(cursor, UUID(str(row[0])))

    def insert_rating_run(
        self,
        rating_run: StoredRatingRun,
        rating_lines: tuple[StoredRatingLine, ...],
    ) -> StoredRatingRun:
        """Persist one rating result and all normalized lines atomically."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.rating_run
                    (rating_run_id, tenant_account_id, rate_card_id,
                     rate_card_version, window_started_at, window_ended_at,
                     usage_snapshot_hash, currency_code, rated_total_amount,
                     recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING rating_run_id
                """,
                (
                    rating_run.rating_run_id,
                    rating_run.tenant_account_id,
                    rating_run.rate_card_id,
                    rating_run.rate_card_version,
                    rating_run.window_started_at,
                    rating_run.window_ended_at,
                    rating_run.usage_snapshot_hash,
                    rating_run.currency_code,
                    rating_run.rated_total_amount,
                    rating_run.recorded_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT rating_run_id
                    FROM billing_core.rating_run
                    WHERE tenant_account_id = %s
                      AND window_started_at = %s
                      AND window_ended_at = %s
                      AND rate_card_id = %s
                      AND usage_snapshot_hash = %s
                      AND rate_card_version = %s
                    """,
                    (
                        rating_run.tenant_account_id,
                        rating_run.window_started_at,
                        rating_run.window_ended_at,
                        rating_run.rate_card_id,
                        rating_run.usage_snapshot_hash,
                        rating_run.rate_card_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:  # pragma: no cover - the primary key conflict is not an identity replay
                    raise ValueError("rating run identity already belongs to another result")
                return self._fetch_rating_run(cursor, UUID(str(row[0])))
            for line in rating_lines:
                cursor.execute(
                    """
                    INSERT INTO billing_core.rating_line
                        (rating_line_id, rating_run_id, tenant_account_id,
                         billing_account_id, billing_account_reference,
                         meter_definition_id, meter_code, unit_code,
                         rated_quantity, unit_price_amount, line_total_amount,
                         line_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        line.rating_line_id,
                        line.rating_run_id,
                        line.tenant_account_id,
                        line.billing_account_id,
                        line.billing_account_reference,
                        line.meter_definition_id,
                        line.meter_code,
                        line.unit_code,
                        line.rated_quantity,
                        line.unit_price_amount,
                        line.line_total_amount,
                        line.line_number,
                    ),
                )
            return self._fetch_rating_run(cursor, rating_run.rating_run_id)

    def get_rating_run(self, rating_run_id: UUID) -> StoredRatingRun | None:
        """Return one stored rating result by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT rating_run_id
                FROM billing_core.rating_run
                WHERE rating_run_id = %s
                """,
                (rating_run_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_rating_run(cursor, UUID(str(row[0])))

    def list_rating_runs(
        self, tenant_account_id: UUID | None = None
    ) -> tuple[StoredRatingRun, ...]:
        """Return stored rating results, optionally limited to one tenant."""
        with self._cursor() as cursor:
            if tenant_account_id is None:
                cursor.execute(
                    """
                    SELECT rating_run_id
                    FROM billing_core.rating_run
                    ORDER BY recorded_at, rating_run_id
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT rating_run_id
                    FROM billing_core.rating_run
                    WHERE tenant_account_id = %s
                    ORDER BY recorded_at, rating_run_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(
                self._fetch_rating_run(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def find_invoice_draft(
        self, tenant_account_id: UUID, rating_run_id: UUID
    ) -> StoredInvoiceDraft | None:
        """Return one tenant-scoped invoice draft by rating identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT invoice_draft_id
                FROM billing_core.invoice_draft
                WHERE tenant_account_id = %s AND rating_run_id = %s
                """,
                (tenant_account_id, rating_run_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_invoice_draft(cursor, UUID(str(row[0])))

    def insert_invoice_draft(
        self,
        invoice_draft: StoredInvoiceDraft,
        invoice_draft_lines: tuple[StoredInvoiceDraftLine, ...],
    ) -> StoredInvoiceDraft:
        """Persist one invoice draft and its copied rating lines atomically."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.invoice_draft
                    (invoice_draft_id, tenant_account_id, rating_run_id,
                     usage_snapshot_hash, currency_code, invoice_draft_status,
                     drafted_total_amount, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING invoice_draft_id
                """,
                (
                    invoice_draft.invoice_draft_id,
                    invoice_draft.tenant_account_id,
                    invoice_draft.rating_run_id,
                    invoice_draft.usage_snapshot_hash,
                    invoice_draft.currency_code,
                    invoice_draft.invoice_draft_status,
                    invoice_draft.drafted_total_amount,
                    invoice_draft.recorded_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT invoice_draft_id
                    FROM billing_core.invoice_draft
                    WHERE tenant_account_id = %s AND rating_run_id = %s
                    """,
                    (invoice_draft.tenant_account_id, invoice_draft.rating_run_id),
                )
                row = cursor.fetchone()
                if row is None:  # pragma: no cover - the primary key conflict is not a replay
                    raise ValueError("invoice draft identity already belongs to another draft")
                return self._fetch_invoice_draft(cursor, UUID(str(row[0])))
            for line in invoice_draft_lines:
                cursor.execute(
                    """
                    INSERT INTO billing_core.invoice_draft_line
                        (invoice_draft_line_id, invoice_draft_id, tenant_account_id,
                         billing_account_id, billing_account_reference,
                         meter_definition_id, line_number, meter_code, unit_code,
                         rated_quantity, unit_price_amount, line_total_amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        line.invoice_draft_line_id,
                        line.invoice_draft_id,
                        line.tenant_account_id,
                        line.billing_account_id,
                        line.billing_account_reference,
                        line.meter_definition_id,
                        line.line_number,
                        line.meter_code,
                        line.unit_code,
                        line.rated_quantity,
                        line.unit_price_amount,
                        line.line_total_amount,
                    ),
                )
            return self._fetch_invoice_draft(cursor, invoice_draft.invoice_draft_id)

    def list_invoice_drafts(
        self, tenant_account_id: UUID
    ) -> tuple[StoredInvoiceDraft, ...]:
        """Return invoice drafts limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT invoice_draft_id
                FROM billing_core.invoice_draft
                WHERE tenant_account_id = %s
                ORDER BY recorded_at, invoice_draft_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_invoice_draft(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def get_invoice_draft(self, invoice_draft_id: UUID) -> StoredInvoiceDraft | None:
        """Return one invoice draft by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT invoice_draft_id
                FROM billing_core.invoice_draft
                WHERE invoice_draft_id = %s
                """,
                (invoice_draft_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_invoice_draft(cursor, UUID(str(row[0])))

    def find_tax_assessment_for_draft(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredTaxAssessment | None:
        """Return the optional tax snapshot for one tenant draft."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT assessment.tax_assessment_id, assessment.tenant_account_id,
                       assessment.invoice_draft_id, assessment.tax_rate_version_id,
                       assessment.tax_assessment_contract_version, assessment.tax_code,
                       assessment.tax_rate, assessment.currency_code,
                       assessment.tax_exclusive_amount, assessment.tax_amount,
                       assessment.tax_inclusive_amount, assessment.source_payload_hash,
                       assessment.assessed_at, version.version_number
                FROM billing_core.tax_assessment AS assessment
                JOIN billing_core.tax_rate_version AS version
                  ON version.tenant_account_id = assessment.tenant_account_id
                 AND version.tax_rate_version_id = assessment.tax_rate_version_id
                WHERE assessment.tenant_account_id = %s
                  AND assessment.invoice_draft_id = %s
                """,
                (tenant_account_id, invoice_draft_id),
            )
            row = cursor.fetchone()
        return None if row is None else self._tax_assessment_from_row(row)

    def find_issued_invoice(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredIssuedInvoice | None:
        """Return one same-tenant issued snapshot for a draft."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_id
                FROM billing_core.issued_invoice
                WHERE tenant_account_id = %s AND invoice_draft_id = %s
                """,
                (tenant_account_id, invoice_draft_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_issued_invoice(cursor, UUID(str(row[0])))

    def get_issued_invoice(self, issued_invoice_id: UUID) -> StoredIssuedInvoice | None:
        """Return one issued snapshot by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_id
                FROM billing_core.issued_invoice
                WHERE issued_invoice_id = %s
                """,
                (issued_invoice_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_issued_invoice(cursor, UUID(str(row[0])))

    def list_issued_invoices_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedInvoice, ...]:
        """Return issued snapshots limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_id
                FROM billing_core.issued_invoice
                WHERE tenant_account_id = %s
                ORDER BY issued_at, issued_invoice_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_issued_invoice(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_issued_invoice(
        self,
        issued_invoice: StoredIssuedInvoice,
        issued_invoice_lines: tuple[StoredIssuedInvoiceLine, ...],
    ) -> StoredIssuedInvoice:
        """Persist one invoice snapshot and its lines in one transaction."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.issued_invoice
                    (issued_invoice_id, tenant_account_id, invoice_draft_id,
                     issued_invoice_contract_version, rating_run_id,
                     usage_snapshot_hash, source_payload_hash, currency_code,
                     tax_exclusive_amount, tax_amount, tax_inclusive_amount,
                     issued_invoice_status, issued_at, due_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING issued_invoice_id
                """,
                (
                    issued_invoice.issued_invoice_id,
                    issued_invoice.tenant_account_id,
                    issued_invoice.invoice_draft_id,
                    issued_invoice.issued_invoice_contract_version,
                    issued_invoice.rating_run_id,
                    issued_invoice.usage_snapshot_hash,
                    issued_invoice.source_payload_hash,
                    issued_invoice.currency_code,
                    issued_invoice.tax_exclusive_amount,
                    issued_invoice.tax_amount,
                    issued_invoice.tax_inclusive_amount,
                    issued_invoice.issued_invoice_status,
                    issued_invoice.issued_at,
                    issued_invoice.due_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT issued_invoice_id
                    FROM billing_core.issued_invoice
                    WHERE tenant_account_id = %s AND invoice_draft_id = %s
                    """,
                    (issued_invoice.tenant_account_id, issued_invoice.invoice_draft_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("issued invoice identity already belongs to another snapshot")
                return self._fetch_issued_invoice(cursor, UUID(str(row[0])))
            for line in issued_invoice_lines:
                cursor.execute(
                    """
                    INSERT INTO billing_core.issued_invoice_line
                        (issued_invoice_line_id, issued_invoice_id, tenant_account_id,
                         line_number, billing_account_reference, meter_code, unit_code,
                         rated_quantity, unit_price_amount, line_total_amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        line.issued_invoice_line_id,
                        line.issued_invoice_id,
                        line.tenant_account_id,
                        line.line_number,
                        line.billing_account_reference,
                        line.meter_code,
                        line.unit_code,
                        line.rated_quantity,
                        line.unit_price_amount,
                        line.line_total_amount,
                    ),
                )
            return self._fetch_issued_invoice(cursor, issued_invoice.issued_invoice_id)

    def find_webhook_outbox_event(
        self,
        tenant_account_id: UUID,
        event_type_code: str,
        source_id: UUID,
        payload_hash: str,
    ) -> StoredWebhookOutboxEvent | None:
        """Return one tenant-scoped outbox identity, if it exists."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT outbox_event_id
                FROM billing_core.webhook_outbox_event
                WHERE tenant_account_id = %s
                  AND event_type_code = %s
                  AND source_id = %s
                  AND payload_hash = %s
                """,
                (tenant_account_id, event_type_code, source_id, payload_hash),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))

    def get_webhook_outbox_event(
        self, outbox_event_id: UUID
    ) -> StoredWebhookOutboxEvent | None:
        """Return one outbox row by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT outbox_event_id
                FROM billing_core.webhook_outbox_event
                WHERE outbox_event_id = %s
                """,
                (outbox_event_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))

    def insert_webhook_outbox_event(
        self, outbox_event: StoredWebhookOutboxEvent
    ) -> StoredWebhookOutboxEvent:
        """Persist one outbox event, returning the identity replay."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.webhook_outbox_event
                    (outbox_event_id, tenant_account_id, event_type_code,
                     payload_hash, source_id, occurred_at, delivery_status,
                     payload_json, enqueued_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING outbox_event_id
                """,
                (
                    outbox_event.outbox_event_id,
                    outbox_event.tenant_account_id,
                    outbox_event.event_type_code,
                    outbox_event.payload_hash,
                    outbox_event.source_id,
                    outbox_event.occurred_at,
                    outbox_event.delivery_status,
                    outbox_event.payload_json,
                    outbox_event.enqueued_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT outbox_event_id
                    FROM billing_core.webhook_outbox_event
                    WHERE tenant_account_id = %s
                      AND event_type_code = %s
                      AND source_id = %s
                      AND payload_hash = %s
                    """,
                    (
                        outbox_event.tenant_account_id,
                        outbox_event.event_type_code,
                        outbox_event.source_id,
                        outbox_event.payload_hash,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("outbox event identity already belongs to another row")
            return self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))

    def list_pending_webhook_outbox_events(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookOutboxEvent, ...]:
        """Return pending outbox events limited to one tenant."""
        return self._list_webhook_outbox_events(tenant_account_id, pending_only=True)

    def list_webhook_outbox_events_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookOutboxEvent, ...]:
        """Return all outbox events limited to one tenant."""
        return self._list_webhook_outbox_events(tenant_account_id, pending_only=False)

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
                       billing_account_reference, account_status_code
                FROM billing_core.billing_account
                WHERE tenant_account_id = %s AND billing_account_code = %s
                """,
                (tenant.tenant_account_id, _resource_code(reference)),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(reference)
        return BillingAccount(
            UUID(str(row[0])), UUID(str(row[1])), row[3], row[2], row[4]
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
    def _rate_card_from_row(row: tuple[Any, ...]) -> StoredRateCard:
        """Decode one price-book header row."""
        return StoredRateCard(
            UUID(str(row[0])),
            UUID(str(row[1])),
            row[2],
            row[3],
            row[4],
        )

    @staticmethod
    def _rate_card_line_from_row(row: tuple[Any, ...]) -> StoredRateCardLine:
        """Decode one normalized price-book line row."""
        return StoredRateCardLine(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
        )

    @staticmethod
    def _tax_rate_schedule_from_row(row: tuple[Any, ...]) -> StoredTaxRateSchedule:
        """Decode one tax-rate schedule row."""
        return StoredTaxRateSchedule(
            UUID(str(row[0])), UUID(str(row[1])), row[2], row[3]
        )

    @staticmethod
    def _tax_rate_version_from_row(row: tuple[Any, ...]) -> StoredTaxRateVersion:
        """Decode one published tax-rate version row."""
        return StoredTaxRateVersion(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
        )

    def _fetch_tax_assessment(
        self, cursor: Any, tax_assessment_id: UUID
    ) -> StoredTaxAssessment:
        """Hydrate one tax assessment with its published version number."""
        cursor.execute(
            """
            SELECT assessment.tax_assessment_id, assessment.tenant_account_id,
                   assessment.invoice_draft_id, assessment.tax_rate_version_id,
                   assessment.tax_assessment_contract_version, assessment.tax_code,
                   assessment.tax_rate, assessment.currency_code,
                   assessment.tax_exclusive_amount, assessment.tax_amount,
                   assessment.tax_inclusive_amount, assessment.source_payload_hash,
                   assessment.assessed_at, version.version_number
            FROM billing_core.tax_assessment AS assessment
            JOIN billing_core.tax_rate_version AS version
              ON version.tenant_account_id = assessment.tenant_account_id
             AND version.tax_rate_version_id = assessment.tax_rate_version_id
            WHERE assessment.tax_assessment_id = %s
            """,
            (tax_assessment_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(tax_assessment_id)
        return self._tax_assessment_from_row(row)

    def _rate_card_version_from_cursor(
        self, cursor: Any, row: tuple[Any, ...]
    ) -> StoredRateCardVersion:
        """Decode a published version and its lines on one transaction cursor."""
        cursor.execute(
            """
            SELECT rate_card_line_id, tenant_account_id, rate_card_version_id,
                   metric_code, unit_amount, currency_code
            FROM billing_core.rate_card_line
            WHERE tenant_account_id = %s AND rate_card_version_id = %s
            ORDER BY metric_code, rate_card_line_id
            """,
            (row[1], row[0]),
        )
        return StoredRateCardVersion(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            tuple(self._rate_card_line_from_row(line) for line in cursor.fetchall()),
        )

    def _fetch_rating_run(self, cursor: Any, rating_run_id: UUID) -> StoredRatingRun:
        """Hydrate one rating run and its immutable lines."""
        cursor.execute(
            """
            SELECT run.rating_run_id, run.tenant_account_id, run.rate_card_id,
                   run.rate_card_version, run.window_started_at, run.window_ended_at,
                   run.usage_snapshot_hash, run.currency_code, run.rated_total_amount,
                   run.recorded_at, card.rate_card_name, card.rate_card_code
            FROM billing_core.rating_run AS run
            JOIN billing_core.rate_card AS card
              ON card.tenant_account_id = run.tenant_account_id
             AND card.rate_card_id = run.rate_card_id
            WHERE run.rating_run_id = %s
            """,
            (rating_run_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - callers select an existing run
            raise KeyError(rating_run_id)
        cursor.execute(
            """
            SELECT rating_line_id, rating_run_id, tenant_account_id,
                   billing_account_id, billing_account_reference,
                   meter_definition_id, meter_code, unit_code,
                   rated_quantity, unit_price_amount, line_total_amount,
                   line_number
            FROM billing_core.rating_line
            WHERE tenant_account_id = %s AND rating_run_id = %s
            ORDER BY line_number
            """,
            (row[1], row[0]),
        )
        lines = tuple(
            StoredRatingLine(
                UUID(str(line[0])),
                UUID(str(line[1])),
                UUID(str(line[2])),
                UUID(str(line[3])),
                line[4],
                UUID(str(line[5])),
                line[6],
                line[7],
                line[8],
                line[9],
                line[10],
                line[11],
            )
            for line in cursor.fetchall()
        )
        return StoredRatingRun(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[10] or row[11],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            lines,
        )

    def _fetch_invoice_draft(self, cursor: Any, invoice_draft_id: UUID) -> StoredInvoiceDraft:
        """Hydrate one invoice draft and its immutable copied lines."""
        cursor.execute(
            """
            SELECT invoice_draft_id, tenant_account_id, rating_run_id,
                   usage_snapshot_hash, currency_code, invoice_draft_status,
                   drafted_total_amount, recorded_at
            FROM billing_core.invoice_draft
            WHERE invoice_draft_id = %s
            """,
            (invoice_draft_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - callers select an existing draft
            raise KeyError(invoice_draft_id)
        cursor.execute(
            """
            SELECT invoice_draft_line_id, invoice_draft_id, tenant_account_id,
                   billing_account_id, billing_account_reference,
                   meter_definition_id, meter_code, unit_code, rated_quantity,
                   unit_price_amount, line_total_amount, line_number
            FROM billing_core.invoice_draft_line
            WHERE tenant_account_id = %s AND invoice_draft_id = %s
            ORDER BY line_number
            """,
            (row[1], row[0]),
        )
        lines = tuple(
            StoredInvoiceDraftLine(
                UUID(str(line[0])),
                UUID(str(line[1])),
                UUID(str(line[2])),
                UUID(str(line[3])),
                line[4],
                UUID(str(line[5])),
                line[6],
                line[7],
                line[8],
                line[9],
                line[10],
                line[11],
            )
            for line in cursor.fetchall()
        )
        return StoredInvoiceDraft(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            lines,
        )

    @staticmethod
    def _tax_assessment_from_row(row: tuple[Any, ...]) -> StoredTaxAssessment:
        """Decode one persisted tax assessment snapshot."""
        return StoredTaxAssessment(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
            row[13],
        )

    def _fetch_issued_invoice(
        self, cursor: Any, issued_invoice_id: UUID
    ) -> StoredIssuedInvoice:
        """Hydrate one issued snapshot and its immutable lines."""
        cursor.execute(
            """
            SELECT issued_invoice_id, tenant_account_id, invoice_draft_id,
                   issued_invoice_contract_version, rating_run_id,
                   usage_snapshot_hash, source_payload_hash, currency_code,
                   tax_exclusive_amount, tax_amount, tax_inclusive_amount,
                   issued_invoice_status, issued_at, due_at
            FROM billing_core.issued_invoice
            WHERE issued_invoice_id = %s
            """,
            (issued_invoice_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(issued_invoice_id)
        cursor.execute(
            """
            SELECT issued_invoice_line_id, issued_invoice_id, tenant_account_id,
                   line_number, billing_account_reference, meter_code, unit_code,
                   rated_quantity, unit_price_amount, line_total_amount
            FROM billing_core.issued_invoice_line
            WHERE tenant_account_id = %s AND issued_invoice_id = %s
            ORDER BY line_number
            """,
            (row[1], row[0]),
        )
        lines = tuple(
            StoredIssuedInvoiceLine(
                UUID(str(line[0])),
                UUID(str(line[1])),
                UUID(str(line[2])),
                line[3],
                line[4],
                line[5],
                line[6],
                line[7],
                line[8],
                line[9],
            )
            for line in cursor.fetchall()
        )
        return StoredIssuedInvoice(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
            row[13],
            lines,
        )

    @staticmethod
    def _webhook_outbox_from_row(row: tuple[Any, ...]) -> StoredWebhookOutboxEvent:
        """Decode one normalized webhook outbox row."""
        return StoredWebhookOutboxEvent(
            UUID(str(row[0])),
            UUID(str(row[1])),
            row[2],
            row[3],
            UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            row[8],
        )

    def _fetch_webhook_outbox_event(
        self, cursor: Any, outbox_event_id: UUID
    ) -> StoredWebhookOutboxEvent:
        """Hydrate one outbox event."""
        cursor.execute(
            """
            SELECT outbox_event_id, tenant_account_id, event_type_code,
                   payload_hash, source_id, occurred_at, delivery_status,
                   payload_json, enqueued_at
            FROM billing_core.webhook_outbox_event
            WHERE outbox_event_id = %s
            """,
            (outbox_event_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(outbox_event_id)
        return self._webhook_outbox_from_row(row)

    def _list_webhook_outbox_events(
        self, tenant_account_id: UUID, *, pending_only: bool
    ) -> tuple[StoredWebhookOutboxEvent, ...]:
        """List outbox events with one explicit tenant predicate."""
        with self._cursor() as cursor:
            if pending_only:
                cursor.execute(
                    """
                    SELECT outbox_event_id, tenant_account_id, event_type_code,
                           payload_hash, source_id, occurred_at, delivery_status,
                           payload_json, enqueued_at
                    FROM billing_core.webhook_outbox_event
                    WHERE tenant_account_id = %s AND delivery_status = 'pending'
                    ORDER BY enqueued_at, outbox_event_id
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT outbox_event_id, tenant_account_id, event_type_code,
                           payload_hash, source_id, occurred_at, delivery_status,
                           payload_json, enqueued_at
                    FROM billing_core.webhook_outbox_event
                    WHERE tenant_account_id = %s
                    ORDER BY enqueued_at, outbox_event_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(self._webhook_outbox_from_row(row) for row in cursor.fetchall())

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
