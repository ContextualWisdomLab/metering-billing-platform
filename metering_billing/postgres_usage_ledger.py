"""PostgreSQL repository for the durable usage-to-invoice vertical slice.

The repository owns catalog rows, immutable usage facts, rating runs, invoice
drafts, issued invoices, unused issued-invoice voids, issued credit notes,
unused issued-credit-note voids, credit-note applications, parked leftover
``unapplied_cash``, leftover-apply ``unapplied_cash_application``, leftover
refund ``unapplied_cash_refund``, collection cases, collection-dispute
holds, payment and
credit facts, journal proposals, published spend budgets, and the atomic
webhook outbox used by the first commercial path. Every public operation uses
the supplied PostgreSQL connection; the implementation never falls back to an
in-memory copy. Provider capture and remaining exception repositories remain
subsequent slices of the persistence port.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import re
from threading import RLock
from typing import Any, Iterator
from uuid import UUID

from metering_billing.errors import (
    LateAdjustmentApplicationTargetPeriodNotOpen,
    LateAdjustmentRatingTargetPeriodNotOpen,
    RejectionReasonCode,
    UsageEventConflict,
)
from metering_billing.exact_decimal import (
    format_exact_decimal,
    parse_exact_decimal,
    require_postable_journal_line_amounts,
)
from metering_billing.period_close import (
    BillingPeriod,
    BillingPeriodStatus,
    BillingPeriodTransition,
    FxConversion,
    FxRate,
    LateAdjustment,
    ReconciliationException,
    ReconciliationExceptionAging,
    ReconciliationEvidence,
    ReconciliationLine,
    ReconciliationRun,
    ReconciliationResolution,
    age_reconciliation_exception,
)
from metering_billing.usage_ledger import (
    CURRENCY_CODE_PATTERN,
    SOURCE_PAYLOAD_HASH_PATTERN,
    BillingAccount,
    BillingPrincipal,
    CredentialAssignment,
    CredentialRecord,
    MemoryUsageLedger,
    MeterDefinition,
    MeterQualityRule,
    StoredCollectionCase,
    StoredCollectionDispute,
    StoredCollectionDunningEvent,
    StoredCreditAdjustment,
    StoredCollectionCaseSettlement,
    StoredCollectionWriteOff,
    StoredInvoiceDraft,
    StoredInvoiceDraftLine,
    StoredIngestionReceipt,
    StoredCreditNoteApplication,
    StoredLateAdjustmentApplication,
    StoredLateAdjustmentRating,
    StoredIssuedCreditNote,
    StoredIssuedCreditNoteVoid,
    StoredIssuedInvoice,
    StoredIssuedInvoiceLine,
    StoredIssuedInvoiceVoid,
    StoredUnappliedCash,
    StoredUnappliedCashApplication,
    StoredUnappliedCashRefund,
    StoredJournalProposal,
    StoredJournalProposalLine,
    StoredRateCard,
    StoredRateCardLine,
    StoredRateCardVersion,
    StoredRatingLine,
    StoredRatingRun,
    StoredTaxRateSchedule,
    StoredTaxRateVersion,
    StoredTenantApiCredential,
    StoredTaxAssessment,
    StoredPaymentIntent,
    StoredPaymentReceipt,
    StoredSpendBudget,
    StoredUsageEvent,
    StoredUsageMeasurement,
    StoredWebhookDeliveryAttempt,
    StoredWebhookOutboxEvent,
    StoredWebhookSubscription,
    TenantAccount,
    _validate_audit_timestamp,
    _require_tenant_scoped_reference,
    _resource_code,
    _single_urn_segment,
    generate_record_id,
)


MIGRATION_HISTORY_TABLE = "public.metering_billing_schema_migration"
"""Migration-history table mirrored from ``scripts/migrate_postgres.py``."""


def _same_late_adjustment_application(
    stored: StoredLateAdjustmentApplication,
    incoming: StoredLateAdjustmentApplication,
) -> bool:
    """Compare replay identity and immutable source fields, not first-writer audit data."""
    return (
        stored.tenant_account_id == incoming.tenant_account_id
        and stored.late_adjustment_id == incoming.late_adjustment_id
        and stored.target_period_id == incoming.target_period_id
        and format(stored.adjustment_amount, "f")
        == format(incoming.adjustment_amount, "f")
        and stored.currency_code == incoming.currency_code
        and stored.late_adjustment_application_contract_version
        == incoming.late_adjustment_application_contract_version
        and stored.late_adjustment_application_status
        == incoming.late_adjustment_application_status
    )


def _same_late_adjustment_rating(
    stored: StoredLateAdjustmentRating,
    incoming: StoredLateAdjustmentRating,
) -> bool:
    """Compare every immutable rating field except its generated id."""
    return (
        stored.tenant_account_id == incoming.tenant_account_id
        and stored.late_adjustment_application_id
        == incoming.late_adjustment_application_id
        and stored.late_adjustment_id == incoming.late_adjustment_id
        and stored.target_period_id == incoming.target_period_id
        and format(stored.adjustment_amount, "f")
        == format(incoming.adjustment_amount, "f")
        and stored.currency_code == incoming.currency_code
        and stored.late_adjustment_rating_contract_version
        == incoming.late_adjustment_rating_contract_version
        and stored.late_adjustment_rating_status
        == incoming.late_adjustment_rating_status
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
        self.webhook_subscription_secrets: dict[UUID, str] = {}
        # One psycopg connection serializes its transactions; the threaded web
        # tier must funnel every session touch through this reentrant lock so
        # concurrent requests never interleave transaction nesting.
        self._connection_lock = RLock()

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
        self._connection_lock.acquire()
        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    yield cursor
        finally:
            self._connection_lock.release()

    @contextmanager
    def ingestion_transaction(self) -> Iterator[None]:
        """Commit one ingest decision and its audit receipt atomically."""
        self._connection_lock.acquire()
        try:
            if self._transaction_active:
                yield
                return
            self._transaction_active = True
            try:
                with self.connection.transaction():
                    yield
            finally:
                self._transaction_active = False
        finally:
            self._connection_lock.release()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit one multi-record commercial command atomically."""
        self._connection_lock.acquire()
        try:
            if self._transaction_active:
                yield
                return
            self._transaction_active = True
            try:
                with self.connection.transaction():
                    yield
            finally:
                self._transaction_active = False
        finally:
            self._connection_lock.release()

    def close(self) -> None:
        """Close the connection when this repository owns it."""
        if self._owns_connection:
            self.connection.close()

    def migration_history_row_count(self) -> int:
        """Return one cheap liveness-probe row count from the migration history.

        The probe runs through the same connection and transaction conventions
        as every other repository operation, so ``/readyz`` never opens an
        ad-hoc PostgreSQL connection beside the ledger's own session.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM public.metering_billing_schema_migration"
            )
            row = cursor.fetchone()
        if row is None:  # pragma: no cover - COUNT(*) always returns one row
            raise RuntimeError("migration history count did not return a row")
        return int(row[0])

    def get_billing_period(
        self, tenant_reference: str, period_id: UUID
    ) -> BillingPeriod | None:
        """Return one tenant-scoped period aggregate with its transition history."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_billing_period(
                cursor, period_id, tenant_account_id=tenant_account_id
            )

    def insert_billing_period(self, period: BillingPeriod) -> BillingPeriod:
        """Persist a period and append only transitions not already stored."""
        return self._insert_billing_period(period, allow_reconciled=False)

    def get_late_adjustment(
        self, tenant_reference: str, late_adjustment_id: UUID
    ) -> LateAdjustment | None:
        """Return one tenant-scoped immutable later-period adjustment."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_late_adjustment(
                cursor, late_adjustment_id, tenant_account_id
            )

    def insert_late_adjustment(
        self, tenant_reference: str, adjustment: LateAdjustment
    ) -> LateAdjustment:
        """Persist one correction without rewriting either billing-period snapshot."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                INSERT INTO billing_core.late_adjustment
                    (late_adjustment_id, tenant_account_id, source_period_id,
                     target_period_id, adjustment_kind, adjustment_amount,
                     currency_code, source_reference, source_payload_hash,
                     recorded_at, late_adjustment_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING late_adjustment_id
                """,
                (
                    adjustment.late_adjustment_id,
                    tenant_account_id,
                    adjustment.source_period_id,
                    adjustment.target_period_id,
                    adjustment.adjustment_kind.value,
                    adjustment.adjustment_amount,
                    adjustment.currency_code,
                    adjustment.source_reference,
                    adjustment.source_payload_hash,
                    adjustment.recorded_at,
                    adjustment.late_adjustment_contract_version,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT late_adjustment_id
                    FROM billing_core.late_adjustment
                    WHERE tenant_account_id = %s
                      AND (
                          (
                              source_period_id = %s
                              AND target_period_id = %s
                              AND adjustment_kind = %s
                              AND source_payload_hash = %s
                              AND late_adjustment_contract_version = %s
                          )
                          OR source_reference = %s
                      )
                    """,
                    (
                        tenant_account_id,
                        adjustment.source_period_id,
                        adjustment.target_period_id,
                        adjustment.adjustment_kind.value,
                        adjustment.source_payload_hash,
                        adjustment.late_adjustment_contract_version,
                        adjustment.source_reference,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "late adjustment identity conflicts with an existing row"
                    )
            stored = self._fetch_late_adjustment(
                cursor, UUID(str(row[0])), tenant_account_id
            )
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict exposes a row
                raise RuntimeError("late adjustment insert did not return a row")
            stored_contract = stored.as_contract_dict()
            adjustment_contract = adjustment.as_contract_dict()
            stored_contract.pop("late_adjustment_id")
            adjustment_contract.pop("late_adjustment_id")
            if stored_contract != adjustment_contract:
                raise ValueError("late adjustment identity cannot change")
            return stored

    def find_late_adjustment_application(
        self, tenant_account_id: UUID, late_adjustment_id: UUID
    ) -> StoredLateAdjustmentApplication | None:
        """Return one tenant-scoped late-adjustment application, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT late_adjustment_application_id
                FROM billing_core.late_adjustment_application
                WHERE tenant_account_id = %s AND late_adjustment_id = %s
                """,
                (tenant_account_id, late_adjustment_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_late_adjustment_application(
                    cursor, UUID(str(row[0])), tenant_account_id
                )
            )

    def find_late_adjustment_application_ids(
        self, tenant_account_id: UUID, late_adjustment_ids: tuple[UUID, ...]
    ) -> frozenset[UUID]:
        """Return applied late-adjustment IDs for one bounded page."""
        if not late_adjustment_ids:
            return frozenset()
        placeholders = ", ".join("%s" for _ in late_adjustment_ids)
        with self._cursor() as cursor:
            cursor.execute(
                f"""
                SELECT late_adjustment_id
                FROM billing_core.late_adjustment_application
                WHERE tenant_account_id = %s
                  AND late_adjustment_id IN ({placeholders})
                """,
                (tenant_account_id, *late_adjustment_ids),
            )
            return frozenset(UUID(str(row[0])) for row in cursor.fetchall())

    def find_late_adjustment_rating_ids(
        self, tenant_account_id: UUID, late_adjustment_ids: tuple[UUID, ...]
    ) -> frozenset[UUID]:
        """Return rated late-adjustment IDs for one bounded page."""
        if not late_adjustment_ids:
            return frozenset()
        placeholders = ", ".join("%s" for _ in late_adjustment_ids)
        with self._cursor() as cursor:
            cursor.execute(
                f"""
                SELECT late_adjustment_id
                FROM billing_core.late_adjustment_rating
                WHERE tenant_account_id = %s
                  AND late_adjustment_id IN ({placeholders})
                """,
                (tenant_account_id, *late_adjustment_ids),
            )
            return frozenset(UUID(str(row[0])) for row in cursor.fetchall())

    def get_late_adjustment_application(
        self, late_adjustment_application_id: UUID
    ) -> StoredLateAdjustmentApplication | None:
        """Return one late-adjustment application by opaque identifier."""
        with self._cursor() as cursor:
            return self._fetch_late_adjustment_application(
                cursor, late_adjustment_application_id
            )

    def insert_late_adjustment_application(
        self, application: StoredLateAdjustmentApplication
    ) -> StoredLateAdjustmentApplication:
        """Persist one application or return the tenant-scoped replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(application.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            not isinstance(application.adjustment_amount, Decimal)
            or application.adjustment_amount.is_nan()
            or application.adjustment_amount.is_infinite()
            or application.adjustment_amount == 0
        ):
            raise ValueError("adjustment_amount must be a finite non-zero exact decimal")
        amount_text = format(application.adjustment_amount, "f")
        if (
            not re.fullmatch(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", amount_text)
            or len(amount_text) > 40
        ):
            raise ValueError("adjustment_amount must be a canonical exact decimal")
        if application.late_adjustment_application_status != "applied":
            raise ValueError("late_adjustment_application_status must be applied")
        if (
            not isinstance(application.applied_by, str)
            or not application.applied_by.strip()
        ):
            raise ValueError("applied_by must be non-empty")
        if (
            not isinstance(application.authorization_reference, str)
            or not application.authorization_reference.strip()
        ):
            raise ValueError("authorization_reference must be non-empty")
        _validate_audit_timestamp(application.applied_at, "applied_at")
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT late_adjustment_id
                FROM billing_core.late_adjustment
                WHERE late_adjustment_id = %s AND tenant_account_id = %s
                FOR UPDATE
                """,
                (application.late_adjustment_id, application.tenant_account_id),
            )
            cursor.fetchone()
            cursor.execute(
                """
                SELECT period_id
                FROM billing_core.billing_period
                WHERE period_id = %s AND tenant_account_id = %s
                FOR UPDATE
                """,
                (application.target_period_id, application.tenant_account_id),
            )
            if cursor.fetchone() is None:
                raise LateAdjustmentApplicationTargetPeriodNotOpen(
                    "late adjustment application target period must be open"
                )
            cursor.execute(
                """
                SELECT late_adjustment_application_id
                FROM billing_core.late_adjustment_application
                WHERE tenant_account_id = %s AND late_adjustment_id = %s
                """,
                (application.tenant_account_id, application.late_adjustment_id),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                stored = self._fetch_late_adjustment_application(
                    cursor, UUID(str(existing_row[0])), application.tenant_account_id
                )
                if stored is None:  # pragma: no cover - row is locked by this transaction
                    raise RuntimeError(  # pragma: no cover - row is locked by this transaction
                        "late adjustment application did not return a row"
                    )
                if not _same_late_adjustment_application(stored, application):
                    raise ValueError("late adjustment application identity cannot change")
                return stored
            cursor.execute(
                """
                SELECT COALESCE((
                    SELECT transition.to_status
                    FROM billing_core.billing_period_transition AS transition
                    WHERE transition.tenant_account_id = %s
                      AND transition.period_id = %s
                    ORDER BY transition.transition_number DESC
                    LIMIT 1
                ), 'open')
                """,
                (application.tenant_account_id, application.target_period_id),
            )
            if cursor.fetchone()[0] != "open":
                raise LateAdjustmentApplicationTargetPeriodNotOpen(
                    "late adjustment application target period must be open"
                )
            cursor.execute(
                """
                INSERT INTO billing_core.late_adjustment_application
                    (late_adjustment_application_id, tenant_account_id,
                     late_adjustment_id, target_period_id, adjustment_amount,
                     currency_code, applied_by, authorization_reference, applied_at,
                     late_adjustment_application_contract_version,
                     late_adjustment_application_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING late_adjustment_application_id
                """,
                (
                    application.late_adjustment_application_id,
                    application.tenant_account_id,
                    application.late_adjustment_id,
                    application.target_period_id,
                    application.adjustment_amount,
                    application.currency_code,
                    application.applied_by,
                    application.authorization_reference,
                    application.applied_at,
                    application.late_adjustment_application_contract_version,
                    application.late_adjustment_application_status,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "late adjustment application identity conflicts with an existing row"
                )
            stored = self._fetch_late_adjustment_application(
                cursor, UUID(str(row[0])), application.tenant_account_id
            )
            if stored is None:  # pragma: no cover - insert exposes a row
                raise RuntimeError("late adjustment application did not return a row")
            return stored

    def find_late_adjustment_rating(
        self, tenant_account_id: UUID, late_adjustment_id: UUID
    ) -> StoredLateAdjustmentRating | None:
        """Return one tenant-scoped late-adjustment rating, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT late_adjustment_rating_id
                FROM billing_core.late_adjustment_rating
                WHERE tenant_account_id = %s AND late_adjustment_id = %s
                """,
                (tenant_account_id, late_adjustment_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_late_adjustment_rating(
                    cursor, UUID(str(row[0])), tenant_account_id
                )
            )

    def get_late_adjustment_rating(
        self, late_adjustment_rating_id: UUID
    ) -> StoredLateAdjustmentRating | None:
        """Return one late-adjustment rating by opaque identifier."""
        with self._cursor() as cursor:
            return self._fetch_late_adjustment_rating(cursor, late_adjustment_rating_id)

    def insert_late_adjustment_rating(
        self, rating: StoredLateAdjustmentRating
    ) -> StoredLateAdjustmentRating:
        """Persist one rating fact or return its tenant-scoped replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(rating.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            not isinstance(rating.adjustment_amount, Decimal)
            or rating.adjustment_amount.is_nan()
            or rating.adjustment_amount.is_infinite()
            or rating.adjustment_amount == 0
        ):
            raise ValueError("adjustment_amount must be a finite non-zero exact decimal")
        amount_text = format(rating.adjustment_amount, "f")
        if (
            not re.fullmatch(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", amount_text)
            or len(amount_text) > 40
        ):
            raise ValueError("adjustment_amount must be a canonical exact decimal")
        if rating.late_adjustment_rating_status != "rated":
            raise ValueError("late_adjustment_rating_status must be rated")
        for value, field_name in (
            (rating.rated_by, "rated_by"),
            (rating.authorization_reference, "authorization_reference"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT period_id
                FROM billing_core.billing_period
                WHERE period_id = %s AND tenant_account_id = %s
                FOR UPDATE
                """,
                (rating.target_period_id, rating.tenant_account_id),
            )
            if cursor.fetchone() is None:
                raise LateAdjustmentRatingTargetPeriodNotOpen(
                    "late adjustment rating target period must be open"
                )
            cursor.execute(
                """
                SELECT COALESCE((
                    SELECT transition.to_status
                    FROM billing_core.billing_period_transition AS transition
                    WHERE transition.tenant_account_id = %s
                      AND transition.period_id = %s
                    ORDER BY transition.transition_number DESC
                    LIMIT 1
                ), 'open')
                """,
                (rating.tenant_account_id, rating.target_period_id),
            )
            target_status = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT late_adjustment_rating_id
                FROM billing_core.late_adjustment_rating
                WHERE tenant_account_id = %s
                  AND (
                      late_adjustment_application_id = %s
                      OR late_adjustment_id = %s
                  )
                """,
                (
                    rating.tenant_account_id,
                    rating.late_adjustment_application_id,
                    rating.late_adjustment_id,
                ),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                stored = self._fetch_late_adjustment_rating(
                    cursor, UUID(str(existing_row[0])), rating.tenant_account_id
                )
                if stored is None:  # pragma: no cover - row is locked by this transaction
                    raise RuntimeError("late adjustment rating did not return a row")
                if not _same_late_adjustment_rating(stored, rating):
                    raise ValueError("late adjustment rating identity cannot change")
                return stored
            if target_status != "open":
                raise LateAdjustmentRatingTargetPeriodNotOpen(
                    "late adjustment rating target period must be open"
                )
            cursor.execute(
                """
                INSERT INTO billing_core.late_adjustment_rating
                    (late_adjustment_rating_id, tenant_account_id,
                     late_adjustment_application_id, late_adjustment_id,
                     target_period_id, adjustment_amount, currency_code,
                     rated_by, authorization_reference, rated_at,
                     late_adjustment_rating_contract_version,
                     late_adjustment_rating_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING late_adjustment_rating_id
                """,
                (
                    rating.late_adjustment_rating_id,
                    rating.tenant_account_id,
                    rating.late_adjustment_application_id,
                    rating.late_adjustment_id,
                    rating.target_period_id,
                    rating.adjustment_amount,
                    rating.currency_code,
                    rating.rated_by,
                    rating.authorization_reference,
                    rating.rated_at,
                    rating.late_adjustment_rating_contract_version,
                    rating.late_adjustment_rating_status,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "late adjustment rating identity conflicts with an existing row"
                )
            stored = self._fetch_late_adjustment_rating(
                cursor, UUID(str(row[0])), rating.tenant_account_id
            )
            if stored is None:  # pragma: no cover - insert or conflict exposes a row
                raise RuntimeError("late adjustment rating did not return a row")
            if not _same_late_adjustment_rating(stored, rating):
                raise ValueError(  # pragma: no cover - inserted values are validated above
                    "late adjustment rating identity cannot change"
                )
            return stored

    def list_late_adjustments(
        self,
        tenant_reference: str,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> tuple[LateAdjustment, ...]:
        """Return ordered late adjustments after an optional bounded cursor."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            query = """
                SELECT late_adjustment_id, source_period_id, target_period_id,
                       adjustment_kind, adjustment_amount, currency_code,
                       source_reference, source_payload_hash, recorded_at,
                       late_adjustment_contract_version
                FROM billing_core.late_adjustment
                WHERE tenant_account_id = %s
            """
            parameters: list[Any] = [tenant_account_id]
            if after is not None:
                query += " AND (recorded_at, late_adjustment_id) > (%s, %s)"
                parameters.extend(after)
            query += " ORDER BY recorded_at, late_adjustment_id"
            if limit is not None:
                query += " LIMIT %s"
                parameters.append(limit)
            cursor.execute(query, tuple(parameters))
            adjustments = tuple(
                LateAdjustment(
                    late_adjustment_id=UUID(str(row[0])),
                    source_period_id=UUID(str(row[1])),
                    target_period_id=UUID(str(row[2])),
                    adjustment_kind=row[3],
                    adjustment_amount=row[4],
                    currency_code=row[5],
                    source_reference=row[6],
                    source_payload_hash=row[7],
                    recorded_at=row[8],
                    late_adjustment_contract_version=row[9],
                )
                for row in cursor.fetchall()
            )
        return adjustments

    def _insert_billing_period(
        self, period: BillingPeriod, *, allow_reconciled: bool
    ) -> BillingPeriod:
        """Persist a period, allowing reconciled only from the gated command."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, period.tenant_reference
            )
            # Serialize transition writers with the application trigger's
            # target-period FOR UPDATE lock before appending any transition.
            cursor.execute(
                """
                SELECT period_id
                FROM billing_core.billing_period
                WHERE tenant_account_id = %s AND period_id = %s
                FOR UPDATE
                """,
                (tenant_account_id, period.period_id),
            )
            cursor.execute(
                """
                INSERT INTO billing_core.billing_period
                    (period_id, tenant_account_id, period_start, period_end,
                     opened_at, opened_by, period_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (period_id) DO NOTHING
                """,
                (
                    period.period_id,
                    tenant_account_id,
                    period.period_start,
                    period.period_end,
                    period.opened_at,
                    period.opened_by,
                    period.period_contract_version,
                ),
            )
            existing = self._fetch_billing_period(cursor, period.period_id, lock=True)
            if (
                existing is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError("billing period insert did not return a row")
            if (
                existing.tenant_reference != period.tenant_reference
                or existing.period_start != period.period_start
                or existing.period_end != period.period_end
                or existing.opened_at != period.opened_at
                or existing.opened_by != period.opened_by
                or existing.period_contract_version != period.period_contract_version
            ):
                raise ValueError(
                    "billing period identity cannot change after persistence"
                )
            prefix = period.transitions[: len(existing.transitions)]
            if existing.transitions != prefix:
                raise ValueError(
                    "billing period transition history cannot be rewritten"
                )
            new_transitions = period.transitions[len(existing.transitions) :]
            if not allow_reconciled and any(
                transition.to_status == BillingPeriodStatus.RECONCILED
                for transition in new_transitions
            ):
                raise ValueError("reconciled periods require reconcile_billing_period")
            for transition_number, transition in enumerate(
                new_transitions,
                start=len(existing.transitions) + 1,
            ):
                cursor.execute(
                    """
                    INSERT INTO billing_core.billing_period_transition
                        (transition_id, tenant_account_id, period_id, transition_number, from_status,
                         to_status, actor_reference, authorization_reference, transition_reason,
                         transitioned_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (transition_id) DO NOTHING
                    """,
                    (
                        transition.transition_id,
                        tenant_account_id,
                        period.period_id,
                        transition_number,
                        transition.from_status.value,
                        transition.to_status.value,
                        transition.actor_reference,
                        transition.authorization_reference,
                        transition.reason,
                        transition.transitioned_at,
                    ),
                )
                cursor.execute(
                    """
                    SELECT transition_number, tenant_account_id, period_id, from_status, to_status,
                           actor_reference, authorization_reference, transition_reason, transitioned_at
                    FROM billing_core.billing_period_transition
                    WHERE transition_id = %s
                    """,
                    (transition.transition_id,),
                )
                row = cursor.fetchone()
                if row is None or row != (
                    transition_number,
                    tenant_account_id,
                    period.period_id,
                    transition.from_status.value,
                    transition.to_status.value,
                    transition.actor_reference,
                    transition.authorization_reference,
                    transition.reason,
                    transition.transitioned_at,
                ):
                    raise ValueError("billing period transition identity cannot change")
            stored = self._fetch_billing_period(cursor, period.period_id)
            if (
                stored is None
            ):  # pragma: no cover - locked row remains present in this transaction
                raise RuntimeError("billing period disappeared after persistence")
            return stored

    def reconcile_billing_period(
        self,
        tenant_reference: str,
        period_id: UUID,
        *,
        actor_reference: str,
        authorization_reference: str,
        reason: str,
        transitioned_at: datetime,
        transition_id: UUID | None = None,
    ) -> BillingPeriod:
        """Append ``reconciled`` only after the latest run has no open exceptions."""
        with self.transaction():
            with self._cursor() as cursor:
                tenant_account_id = self._tenant_account_id_with_cursor(
                    cursor, tenant_reference
                )
                period = self._fetch_billing_period(
                    cursor,
                    period_id,
                    lock=True,
                    tenant_account_id=tenant_account_id,
                )
                if period is None:
                    raise KeyError(period_id)
                if period.status != BillingPeriodStatus.SOFT_CLOSED:
                    raise ValueError("period must be soft_closed before reconciliation")
                cursor.execute(
                    """
                    SELECT run.blocking_exception_count,
                           COUNT(exception.exception_code) AS exception_count,
                           COUNT(exception.exception_code) FILTER (
                               WHERE NOT EXISTS (
                                   SELECT 1
                                   FROM billing_core.reconciliation_resolution AS resolution
                                   WHERE resolution.reconciliation_line_id = exception.reconciliation_line_id
                                     AND resolution.exception_code = exception.exception_code
                               )
                           ) AS unresolved_exception_count,
                           COUNT(DISTINCT run_line.reconciliation_line_id) AS run_line_count,
                           (
                               SELECT COUNT(*)
                               FROM billing_core.reconciliation_line AS period_line
                               WHERE period_line.tenant_account_id = run.tenant_account_id
                                 AND period_line.period_id = run.period_id
                           ) AS period_line_count
                    FROM billing_core.reconciliation_run AS run
                    LEFT JOIN billing_core.reconciliation_run_line AS run_line
                      ON run_line.run_id = run.run_id
                    LEFT JOIN billing_core.reconciliation_exception AS exception
                      ON exception.reconciliation_line_id = run_line.reconciliation_line_id
                    WHERE run.tenant_account_id = %s
                      AND run.period_id = %s
                    GROUP BY run.run_id, run.blocking_exception_count, run.completed_at
                    ORDER BY run.completed_at DESC, run.run_id DESC
                    LIMIT 1
                    """,
                    (tenant_account_id, period_id),
                )
                run = cursor.fetchone()
                if run is None:
                    raise ValueError("a completed reconciliation run is required")
                (
                    blocking_count,
                    exception_count,
                    unresolved_count,
                    run_line_count,
                    period_line_count,
                ) = map(int, run)
                if blocking_count != exception_count:
                    raise ValueError(
                        "reconciliation run exception summary is inconsistent"
                    )
                if run_line_count != period_line_count:
                    raise ValueError(
                        "reconciliation run does not cover every period line"
                    )
                if unresolved_count:
                    raise ValueError(
                        "blocking reconciliation exceptions remain unresolved"
                    )
            reconciled = period.advance(
                BillingPeriodStatus.RECONCILED,
                actor_reference=actor_reference,
                authorization_reference=authorization_reference,
                reason=reason,
                transitioned_at=transitioned_at,
                transition_id=transition_id,
            )
            return self._insert_billing_period(reconciled, allow_reconciled=True)

    def list_billing_periods(self, tenant_reference: str) -> tuple[BillingPeriod, ...]:
        """Return period aggregates for one registered tenant only."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                SELECT period_id
                FROM billing_core.billing_period
                WHERE tenant_account_id = %s
                ORDER BY period_start, period_id
                """,
                (tenant_account_id,),
            )
            periods = tuple(
                self._fetch_billing_period(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )
        return tuple(period for period in periods if period is not None)

    def get_fx_rate(self, fx_rate_id: UUID) -> FxRate | None:
        """Return one immutable exchange-rate evidence row."""
        with self._cursor() as cursor:
            return self._fetch_fx_rate(cursor, fx_rate_id)

    def insert_fx_rate(self, rate: FxRate) -> FxRate:
        """Persist one exact FX rate without replacing an existing snapshot."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.fx_rate
                    (fx_rate_id, rate_source, rate_type, base_currency, quote_currency,
                     fx_rate_value, rate_precision, effective_at, recorded_at,
                     fx_rate_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fx_rate_id) DO NOTHING
                """,
                (
                    rate.fx_rate_id,
                    rate.rate_source,
                    rate.rate_type.value,
                    rate.base_currency,
                    rate.quote_currency,
                    rate.rate,
                    rate.rate_precision,
                    rate.effective_at,
                    rate.recorded_at,
                    rate.fx_rate_contract_version,
                ),
            )
            stored = self._fetch_fx_rate(cursor, rate.fx_rate_id)
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError("FX rate insert did not return a row")
            if stored.as_contract_dict() != rate.as_contract_dict():
                raise ValueError("FX rate identity cannot change after persistence")
            return stored

    def get_fx_conversion(self, fx_conversion_id: UUID) -> FxConversion | None:
        """Return one frozen conversion result and its pinned rate snapshot."""
        with self._cursor() as cursor:
            return self._fetch_fx_conversion(cursor, fx_conversion_id)

    def insert_fx_conversion(self, conversion: FxConversion) -> FxConversion:
        """Persist a conversion only when its copied rate matches the pinned evidence."""
        with self._cursor() as cursor:
            rate = self._fetch_fx_rate(cursor, conversion.fx_rate_id)
            if rate is None:
                raise KeyError(conversion.fx_rate_id)
            if (
                rate.base_currency != conversion.source_currency
                or rate.quote_currency != conversion.quote_currency
                or rate.rate != conversion.rate
                or rate.rate_precision != conversion.rate_precision
            ):
                raise ValueError("FX conversion must match its pinned rate snapshot")
            cursor.execute(
                """
                INSERT INTO billing_core.fx_conversion
                    (fx_conversion_id, fx_rate_id, source_amount, source_currency,
                     quote_amount, quote_currency, quote_minor_units, fx_rate_value,
                     rate_precision, rounding_mode, converted_at,
                     fx_conversion_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fx_conversion_id) DO NOTHING
                """,
                (
                    conversion.fx_conversion_id,
                    conversion.fx_rate_id,
                    conversion.source_amount,
                    conversion.source_currency,
                    conversion.quote_amount,
                    conversion.quote_currency,
                    conversion.quote_minor_units,
                    conversion.rate,
                    conversion.rate_precision,
                    "ROUND_HALF_UP",
                    conversion.converted_at,
                    conversion.fx_conversion_contract_version,
                ),
            )
            stored = self._fetch_fx_conversion(cursor, conversion.fx_conversion_id)
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError("FX conversion insert did not return a row")
            if stored.as_contract_dict() != conversion.as_contract_dict():
                raise ValueError(
                    "FX conversion identity cannot change after persistence"
                )
            return stored

    def get_reconciliation_line(
        self, tenant_reference: str, reconciliation_line_id: UUID
    ) -> ReconciliationLine | None:
        """Return one tenant-scoped reconciliation line with typed exceptions."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_reconciliation_line(
                cursor, reconciliation_line_id, tenant_account_id=tenant_account_id
            )

    def insert_reconciliation_line(
        self, tenant_reference: str, line: ReconciliationLine
    ) -> ReconciliationLine:
        """Persist one tenant-owned line and its exception children atomically."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                SELECT tenant_account_id
                FROM billing_core.billing_period
                WHERE period_id = %s
                """,
                (line.period_id,),
            )
            period = cursor.fetchone()
            if period is None or UUID(str(period[0])) != tenant_account_id:
                raise KeyError(line.period_id)
            cursor.execute(
                """
                INSERT INTO billing_core.reconciliation_line
                    (reconciliation_line_id, tenant_account_id, period_id,
                     provider_account_reference, currency_code, internal_currency_code,
                     provider_currency_code, cash_currency_code, internal_expected_amount,
                     provider_actual_amount, cash_actual_amount, provider_fee_amount,
                     withheld_tax_amount, reserve_amount, expected_cash_amount,
                     reconciliation_line_status, assessed_at,
                     reconciliation_line_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                ON CONFLICT (reconciliation_line_id) DO NOTHING
                RETURNING reconciliation_line_id
                """,
                (
                    line.reconciliation_line_id,
                    tenant_account_id,
                    line.period_id,
                    line.provider_account_reference,
                    line.currency_code,
                    line.internal_currency_code,
                    line.provider_currency_code,
                    line.cash_currency_code,
                    line.internal_expected_amount,
                    line.provider_actual_amount,
                    line.cash_actual_amount,
                    line.provider_fee_amount,
                    line.withheld_tax_amount,
                    line.reserve_amount,
                    line.expected_cash_amount,
                    line.status.value,
                    line.assessed_at,
                    line.reconciliation_line_contract_version,
                ),
            )
            inserted = cursor.fetchone() is not None
            cursor.execute(
                """
                SELECT tenant_account_id, period_id, provider_account_reference,
                       currency_code, internal_currency_code, provider_currency_code,
                       cash_currency_code, internal_expected_amount, provider_actual_amount,
                       cash_actual_amount, provider_fee_amount, withheld_tax_amount,
                       reserve_amount, expected_cash_amount, reconciliation_line_status,
                       assessed_at, reconciliation_line_contract_version
                FROM billing_core.reconciliation_line
                WHERE reconciliation_line_id = %s
                """,
                (line.reconciliation_line_id,),
            )
            parent = cursor.fetchone()
            if (
                parent is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError(
                    "reconciliation line insert did not return a parent row"
                )
            if parent != (
                tenant_account_id,
                line.period_id,
                line.provider_account_reference,
                line.currency_code,
                line.internal_currency_code,
                line.provider_currency_code,
                line.cash_currency_code,
                line.internal_expected_amount,
                line.provider_actual_amount,
                line.cash_actual_amount,
                line.provider_fee_amount,
                line.withheld_tax_amount,
                line.reserve_amount,
                line.expected_cash_amount,
                line.status.value,
                line.assessed_at,
                line.reconciliation_line_contract_version,
            ):
                raise ValueError(
                    "reconciliation line identity cannot change after persistence"
                )
            expected_exceptions = tuple(
                (
                    exception_number,
                    exception.exception_code.value,
                    exception.next_action,
                )
                for exception_number, exception in enumerate(line.exceptions, start=1)
            )
            if not inserted:
                cursor.execute(
                    """
                    SELECT exception_number, exception_code, next_action
                    FROM billing_core.reconciliation_exception
                    WHERE reconciliation_line_id = %s
                    ORDER BY exception_number
                    """,
                    (line.reconciliation_line_id,),
                )
                if tuple(cursor.fetchall()) != expected_exceptions:
                    raise ValueError(
                        "reconciliation line exception history cannot change"
                    )
            else:
                for exception_number, exception in enumerate(line.exceptions, start=1):
                    cursor.execute(
                        """
                        INSERT INTO billing_core.reconciliation_exception
                            (reconciliation_line_id, exception_number, exception_code, next_action)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            line.reconciliation_line_id,
                            exception_number,
                            exception.exception_code.value,
                            exception.next_action,
                        ),
                    )
            existing = self._fetch_reconciliation_line(
                cursor, line.reconciliation_line_id
            )
            if (
                existing is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError("reconciliation line insert did not return a row")
            return existing

    def list_reconciliation_lines(
        self, tenant_reference: str, period_id: UUID | None = None
    ) -> tuple[ReconciliationLine, ...]:
        """Return reconciliation lines for one tenant, optionally one period."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            if period_id is None:
                cursor.execute(
                    """
                    SELECT reconciliation_line_id
                    FROM billing_core.reconciliation_line
                    WHERE tenant_account_id = %s
                    ORDER BY assessed_at, reconciliation_line_id
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT reconciliation_line_id
                    FROM billing_core.reconciliation_line
                    WHERE tenant_account_id = %s AND period_id = %s
                    ORDER BY assessed_at, reconciliation_line_id
                    """,
                    (tenant_account_id, period_id),
                )
            lines = tuple(
                self._fetch_reconciliation_line(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )
        return tuple(line for line in lines if line is not None)

    def list_reconciliation_exception_aging(
        self,
        tenant_reference: str,
        as_of: datetime,
        period_id: UUID | None = None,
    ) -> tuple[ReconciliationExceptionAging, ...]:
        """Return tenant-scoped exception aging derived from immutable lines."""
        return tuple(
            age_reconciliation_exception(line, exception.exception_code, as_of)
            for line in self.list_reconciliation_lines(tenant_reference, period_id)
            for exception in line.exceptions
        )

    def get_reconciliation_evidence(
        self, tenant_reference: str, evidence_id: UUID
    ) -> ReconciliationEvidence | None:
        """Return one tenant-scoped hash-backed evidence record."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_reconciliation_evidence(
                cursor, evidence_id, tenant_account_id=tenant_account_id
            )

    def insert_reconciliation_evidence(
        self, tenant_reference: str, evidence: ReconciliationEvidence
    ) -> ReconciliationEvidence:
        """Persist one tenant-owned evidence record for an existing exception."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.reconciliation_line AS line
                JOIN billing_core.reconciliation_exception AS exception
                  ON exception.reconciliation_line_id = line.reconciliation_line_id
                WHERE line.tenant_account_id = %s
                  AND line.reconciliation_line_id = %s
                  AND exception.exception_code = %s
                """,
                (
                    tenant_account_id,
                    evidence.reconciliation_line_id,
                    evidence.exception_code.value,
                ),
            )
            if cursor.fetchone() is None:
                raise KeyError(evidence.exception_code.value)
            cursor.execute(
                """
                INSERT INTO billing_core.reconciliation_evidence
                    (evidence_id, reconciliation_line_id, exception_code, evidence_kind,
                     evidence_reference, evidence_sha256, captured_by, captured_at,
                     reconciliation_evidence_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (evidence_id) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    evidence.reconciliation_line_id,
                    evidence.exception_code.value,
                    evidence.evidence_kind,
                    evidence.evidence_reference,
                    evidence.evidence_sha256,
                    evidence.captured_by,
                    evidence.captured_at,
                    evidence.reconciliation_evidence_contract_version,
                ),
            )
            stored = self._fetch_reconciliation_evidence(cursor, evidence.evidence_id)
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError(
                    "reconciliation evidence insert did not return a row"
                )
            if stored.as_contract_dict() != evidence.as_contract_dict():
                raise ValueError("reconciliation evidence identity cannot change")
            return stored

    def list_reconciliation_evidence(
        self,
        tenant_reference: str,
        reconciliation_line_id: UUID | None = None,
    ) -> tuple[ReconciliationEvidence, ...]:
        """Return evidence for one tenant, optionally one reconciliation line."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            if reconciliation_line_id is None:
                cursor.execute(
                    """
                    SELECT evidence_id
                    FROM billing_core.reconciliation_evidence AS evidence
                    JOIN billing_core.reconciliation_line AS line
                      ON line.reconciliation_line_id = evidence.reconciliation_line_id
                    WHERE line.tenant_account_id = %s
                    ORDER BY evidence.captured_at, evidence.evidence_id
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT evidence_id
                    FROM billing_core.reconciliation_evidence AS evidence
                    JOIN billing_core.reconciliation_line AS line
                      ON line.reconciliation_line_id = evidence.reconciliation_line_id
                    WHERE line.tenant_account_id = %s
                      AND evidence.reconciliation_line_id = %s
                    ORDER BY evidence.captured_at, evidence.evidence_id
                    """,
                    (tenant_account_id, reconciliation_line_id),
                )
            evidence = tuple(
                self._fetch_reconciliation_evidence(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )
        return tuple(item for item in evidence if item is not None)

    def get_reconciliation_run(
        self, tenant_reference: str, run_id: UUID
    ) -> ReconciliationRun | None:
        """Return one tenant-scoped immutable completed reconciliation run."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_reconciliation_run(
                cursor, run_id, tenant_account_id=tenant_account_id
            )

    def insert_reconciliation_run(
        self, tenant_reference: str, run: ReconciliationRun
    ) -> ReconciliationRun:
        """Persist one tenant-owned run and its ordered line membership atomically."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                SELECT tenant_account_id
                FROM billing_core.billing_period
                WHERE period_id = %s
                """,
                (run.period_id,),
            )
            period = cursor.fetchone()
            if period is None or UUID(str(period[0])) != tenant_account_id:
                raise KeyError(run.period_id)
            cursor.execute(
                """
                INSERT INTO billing_core.reconciliation_run
                    (run_id, tenant_account_id, period_id, started_at, completed_at,
                     blocking_exception_count, reconciliation_run_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run.run_id,
                    tenant_account_id,
                    run.period_id,
                    run.started_at,
                    run.completed_at,
                    run.blocking_exception_count,
                    run.reconciliation_run_contract_version,
                ),
            )
            for line_number, reconciliation_line_id in enumerate(
                run.reconciliation_line_ids, start=1
            ):
                cursor.execute(
                    """
                    SELECT 1
                    FROM billing_core.reconciliation_line
                    WHERE tenant_account_id = %s
                      AND period_id = %s
                      AND reconciliation_line_id = %s
                    """,
                    (tenant_account_id, run.period_id, reconciliation_line_id),
                )
                if cursor.fetchone() is None:
                    raise KeyError(reconciliation_line_id)
                cursor.execute(
                    """
                    INSERT INTO billing_core.reconciliation_run_line
                        (run_id, tenant_account_id, period_id, line_number,
                         reconciliation_line_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, line_number) DO NOTHING
                    """,
                    (
                        run.run_id,
                        tenant_account_id,
                        run.period_id,
                        line_number,
                        reconciliation_line_id,
                    ),
                )
                cursor.execute(
                    """
                    SELECT tenant_account_id, period_id, reconciliation_line_id
                    FROM billing_core.reconciliation_run_line
                    WHERE run_id = %s AND line_number = %s
                    """,
                    (run.run_id, line_number),
                )
                row = cursor.fetchone()
                if row != (
                    tenant_account_id,
                    run.period_id,
                    reconciliation_line_id,
                ):  # pragma: no cover - protected by composite identity constraints
                    raise ValueError("reconciliation run line identity cannot change")
            stored = self._fetch_reconciliation_run(
                cursor, run.run_id, tenant_account_id=tenant_account_id
            )
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError("reconciliation run insert did not return a row")
            if stored.as_contract_dict() != run.as_contract_dict():
                raise ValueError("reconciliation run identity cannot change")
            return stored

    def list_reconciliation_runs(
        self, tenant_reference: str, period_id: UUID | None = None
    ) -> tuple[ReconciliationRun, ...]:
        """Return completed runs for one tenant, optionally one period."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            if period_id is None:
                cursor.execute(
                    """
                    SELECT run_id
                    FROM billing_core.reconciliation_run
                    WHERE tenant_account_id = %s
                    ORDER BY completed_at, run_id
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT run_id
                    FROM billing_core.reconciliation_run
                    WHERE tenant_account_id = %s AND period_id = %s
                    ORDER BY completed_at, run_id
                    """,
                    (tenant_account_id, period_id),
                )
            runs = tuple(
                self._fetch_reconciliation_run(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )
        return tuple(item for item in runs if item is not None)

    def get_reconciliation_resolution(
        self, tenant_reference: str, resolution_id: UUID
    ) -> ReconciliationResolution | None:
        """Return one tenant-scoped immutable maker-checker resolution."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            return self._fetch_reconciliation_resolution(
                cursor, resolution_id, tenant_account_id=tenant_account_id
            )

    def insert_reconciliation_resolution(
        self, tenant_reference: str, resolution: ReconciliationResolution
    ) -> ReconciliationResolution:
        """Persist one tenant-owned exception resolution without replacing approvals."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.reconciliation_line AS line
                JOIN billing_core.reconciliation_exception AS exception
                  ON exception.reconciliation_line_id = line.reconciliation_line_id
                WHERE line.tenant_account_id = %s
                  AND line.reconciliation_line_id = %s
                  AND exception.exception_code = %s
                """,
                (
                    tenant_account_id,
                    resolution.reconciliation_line_id,
                    resolution.exception_code.value,
                ),
            )
            if cursor.fetchone() is None:
                raise KeyError(resolution.exception_code.value)
            cursor.execute(
                """
                INSERT INTO billing_core.reconciliation_resolution
                    (resolution_id, reconciliation_line_id, exception_code,
                     resolution_status, owner_reference, resolution_reason,
                     evidence_reference, maker_reference, checker_reference,
                     resolved_at, reconciliation_resolution_contract_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (resolution_id) DO NOTHING
                """,
                (
                    resolution.resolution_id,
                    resolution.reconciliation_line_id,
                    resolution.exception_code.value,
                    resolution.resolution_status.value,
                    resolution.owner_reference,
                    resolution.resolution_reason,
                    resolution.evidence_reference,
                    resolution.maker_reference,
                    resolution.checker_reference,
                    resolution.resolved_at,
                    resolution.reconciliation_resolution_contract_version,
                ),
            )
            stored = self._fetch_reconciliation_resolution(
                cursor, resolution.resolution_id
            )
            if (
                stored is None
            ):  # pragma: no cover - the insert or conflict must expose a row
                raise RuntimeError(
                    "reconciliation resolution insert did not return a row"
                )
            if stored.as_contract_dict() != resolution.as_contract_dict():
                raise ValueError("reconciliation resolution identity cannot change")
            return stored

    def list_reconciliation_resolutions(
        self,
        tenant_reference: str,
        reconciliation_line_id: UUID | None = None,
    ) -> tuple[ReconciliationResolution, ...]:
        """Return resolution history for one tenant, optionally one line."""
        with self._cursor() as cursor:
            tenant_account_id = self._tenant_account_id_with_cursor(
                cursor, tenant_reference
            )
            if reconciliation_line_id is None:
                cursor.execute(
                    """
                    SELECT resolution_id
                    FROM billing_core.reconciliation_resolution AS resolution
                    JOIN billing_core.reconciliation_line AS line
                      ON line.reconciliation_line_id = resolution.reconciliation_line_id
                    WHERE line.tenant_account_id = %s
                    ORDER BY resolution.resolved_at, resolution.resolution_id
                    """,
                    (tenant_account_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT resolution_id
                    FROM billing_core.reconciliation_resolution AS resolution
                    JOIN billing_core.reconciliation_line AS line
                      ON line.reconciliation_line_id = resolution.reconciliation_line_id
                    WHERE line.tenant_account_id = %s
                      AND resolution.reconciliation_line_id = %s
                    ORDER BY resolution.resolved_at, resolution.resolution_id
                    """,
                    (tenant_account_id, reconciliation_line_id),
                )
            resolutions = tuple(
                self._fetch_reconciliation_resolution(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )
        return tuple(resolution for resolution in resolutions if resolution is not None)

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
            if (
                row is None
            ):  # pragma: no cover - a committed unique row cannot disappear here
                raise RuntimeError("tenant insert did not return a row")
            if (
                row[1] != tenant_reference
            ):  # pragma: no cover - code derives from this URN
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
            if (
                row is None
            ):  # pragma: no cover - database row is protected by the unique key
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
            if (
                row is None
            ):  # pragma: no cover - database row is protected by the unique key
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
            if (
                row is None
            ):  # pragma: no cover - database row is protected by the unique key
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
                if (
                    row is None
                ):  # pragma: no cover - exclusion constraint protects the row
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
            if (
                row is None
            ):  # pragma: no cover - database row is protected by the unique key
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
            if (
                row is None
            ):  # pragma: no cover - database row is protected by the unique key
                raise RuntimeError("meter quality rule insert did not return a row")
            return MeterQualityRule(
                UUID(str(row[0])), UUID(str(row[1])), row[2], row[3]
            )

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
                (
                    tenant_account_id,
                    rate_card_id,
                    source_payload_hash,
                    rate_card_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._rate_card_version_from_cursor(cursor, row)
            )

    def get_rate_card_version(
        self, rate_card_version_id: UUID
    ) -> StoredRateCardVersion | None:
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
            return (
                None
                if row is None
                else self._rate_card_version_from_cursor(cursor, row)
            )

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
                if (
                    row is None
                ):  # pragma: no cover - unique identity protects the version
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
                raise ValueError(
                    "tax-rate schedule identity already belongs to another row"
                )
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
            return tuple(
                self._tax_rate_schedule_from_row(row) for row in cursor.fetchall()
            )

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
                    raise ValueError(
                        "tax-rate version identity already belongs to another row"
                    )
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
            return tuple(
                self._tax_rate_version_from_row(row) for row in cursor.fetchall()
            )

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
            return (
                None
                if row is None
                else self._fetch_tax_assessment(cursor, UUID(str(row[0])))
            )

    def get_tax_assessment(self, tax_assessment_id: UUID) -> StoredTaxAssessment | None:
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
            return (
                None
                if row is None
                else self._fetch_tax_assessment(cursor, UUID(str(row[0])))
            )

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
                    raise ValueError(
                        "tax assessment identity already belongs to another row"
                    )
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
            UUID(str(row[0])),
            UUID(str(row[1])),
            billing_account_reference,
            row[2],
            row[3],
        )
        if account.account_status_code != "active":
            return None, RejectionReasonCode.BILLING_ACCOUNT_NOT_ACTIVE
        return account, None

    def get_billing_account(self, billing_account_id: UUID) -> BillingAccount | None:
        """Return one billing account by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT billing_account_id, tenant_account_id, billing_account_code,
                       billing_account_reference, account_status_code
                FROM billing_core.billing_account
                WHERE billing_account_id = %s
                """,
                (billing_account_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return BillingAccount(
            UUID(str(row[0])), UUID(str(row[1])), row[3], row[2], row[4]
        )

    def resolve_billing_principal(
        self,
        tenant: TenantAccount,
        billing_principal_reference: str,
        occurred_at: datetime,
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
                (
                    tenant.tenant_account_id,
                    billing_principal_reference,
                    occurred_at,
                    occurred_at,
                ),
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
        self,
        tenant_account_id: UUID,
        event_payload_hash: str,
        event_contract_version: int,
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
                existing = self._find_event_with_cursor(
                    cursor, event.tenant_account_id, event
                )
                if existing is None:
                    raise ValueError(
                        "usage event conflict has no classified existing row"
                    )
                if existing.source_event_key == event.source_event_key:
                    if (
                        existing.event_payload_hash == event.event_payload_hash
                        and existing.event_contract_version
                        == event.event_contract_version
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

    def append_ingestion_receipt(
        self, receipt: StoredIngestionReceipt
    ) -> StoredIngestionReceipt:
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
        self,
        tenant_account_id: UUID,
        window_started_at: datetime,
        window_ended_at: datetime,
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
            return (
                None
                if row is None
                else self._fetch_rating_run(cursor, UUID(str(row[0])))
            )

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
                if (
                    row is None
                ):  # pragma: no cover - the primary key conflict is not an identity replay
                    raise ValueError(
                        "rating run identity already belongs to another result"
                    )
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
            return (
                None
                if row is None
                else self._fetch_rating_run(cursor, UUID(str(row[0])))
            )

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
            return (
                None
                if row is None
                else self._fetch_invoice_draft(cursor, UUID(str(row[0])))
            )

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
                if (
                    row is None
                ):  # pragma: no cover - the primary key conflict is not a replay
                    raise ValueError(
                        "invoice draft identity already belongs to another draft"
                    )
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
            return (
                None
                if row is None
                else self._fetch_invoice_draft(cursor, UUID(str(row[0])))
            )

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
            return (
                None
                if row is None
                else self._fetch_issued_invoice(cursor, UUID(str(row[0])))
            )

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
            return (
                None
                if row is None
                else self._fetch_issued_invoice(cursor, UUID(str(row[0])))
            )

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
                    raise ValueError(
                        "issued invoice identity already belongs to another snapshot"
                    )
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

    def get_collection_case(
        self, collection_case_id: UUID
    ) -> StoredCollectionCase | None:
        """Return one collection case by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_case(cursor, UUID(str(row[0])))
            )

    def find_collection_case(
        self, tenant_account_id: UUID, invoice_draft_id: UUID
    ) -> StoredCollectionCase | None:
        """Return one tenant-scoped collection case identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id
                FROM billing_core.collection_case
                WHERE tenant_account_id = %s AND invoice_draft_id = %s
                """,
                (tenant_account_id, invoice_draft_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_case(cursor, UUID(str(row[0])))
            )

    def insert_collection_case(
        self, collection_case: StoredCollectionCase
    ) -> StoredCollectionCase:
        """Persist one positive tenant-scoped collection case or replay it."""
        if collection_case.collection_case_status not in {"open", "dunning"}:
            raise ValueError("collection cases cannot be paid, written off, or posted")
        outstanding_amount = parse_exact_decimal(
            format_exact_decimal(collection_case.outstanding_amount)
        )
        if outstanding_amount <= 0:
            raise ValueError(
                "collection case outstanding must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.collection_case
                    (collection_case_id, tenant_account_id, invoice_draft_id,
                     currency_code, collection_case_status, outstanding_amount, opened_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING collection_case_id
                """,
                (
                    collection_case.collection_case_id,
                    collection_case.tenant_account_id,
                    collection_case.invoice_draft_id,
                    collection_case.currency_code,
                    collection_case.collection_case_status,
                    outstanding_amount,
                    collection_case.opened_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT collection_case_id
                    FROM billing_core.collection_case
                    WHERE tenant_account_id = %s AND invoice_draft_id = %s
                    """,
                    (
                        collection_case.tenant_account_id,
                        collection_case.invoice_draft_id,
                    ),
                )
                row = cursor.fetchone()
                if (
                    row is None
                ):  # pragma: no cover - a valid FK conflict has an identity row
                    raise ValueError(
                        "collection case identity already belongs to another case"
                    )
            return self._fetch_collection_case(cursor, UUID(str(row[0])))

    def list_collection_cases(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionCase, ...]:
        """Return collection cases limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id
                FROM billing_core.collection_case
                WHERE tenant_account_id = %s
                ORDER BY opened_at, collection_case_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_collection_case(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def get_collection_dunning_event(
        self, collection_dunning_event_id: UUID
    ) -> StoredCollectionDunningEvent | None:
        """Return one dunning event by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dunning_event_id
                FROM billing_core.collection_dunning_event
                WHERE collection_dunning_event_id = %s
                """,
                (collection_dunning_event_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_dunning_event(cursor, UUID(str(row[0])))
            )

    def list_collection_dunning_events(
        self, collection_case_id: UUID
    ) -> tuple[StoredCollectionDunningEvent, ...]:
        """Return dunning events for one case in event-number order."""
        return self._list_collection_dunning_events(
            collection_case_id=collection_case_id
        )

    def list_collection_dunning_events_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionDunningEvent, ...]:
        """Return dunning events limited to one tenant."""
        return self._list_collection_dunning_events(tenant_account_id=tenant_account_id)

    def find_collection_dunning_event(
        self, collection_case_id: UUID, dunning_notice_code: str
    ) -> StoredCollectionDunningEvent | None:
        """Return one case and notice identity, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dunning_event_id
                FROM billing_core.collection_dunning_event
                WHERE collection_case_id = %s AND dunning_notice_code = %s
                """,
                (collection_case_id, dunning_notice_code),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_dunning_event(cursor, UUID(str(row[0])))
            )

    def insert_collection_dunning_event(
        self, dunning_event: StoredCollectionDunningEvent
    ) -> StoredCollectionDunningEvent:
        """Append one dunning event; an exact notice replay returns its row."""
        if dunning_event.dunning_notice_code not in {"first_notice", "overdue_notice"}:
            raise ValueError(
                "collection dunning notices must be commercial reminder codes"
            )
        if dunning_event.dunning_event_number < 1:
            raise ValueError("collection dunning event number must be positive")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.collection_dunning_event
                    (collection_dunning_event_id, collection_case_id,
                     tenant_account_id, dunning_event_number, dunning_notice_code,
                     occurred_at)
                SELECT %s, %s, c.tenant_account_id, %s, %s, %s
                FROM billing_core.collection_case AS c
                WHERE c.collection_case_id = %s
                ON CONFLICT DO NOTHING
                RETURNING collection_dunning_event_id
                """,
                (
                    dunning_event.collection_dunning_event_id,
                    dunning_event.collection_case_id,
                    dunning_event.dunning_event_number,
                    dunning_event.dunning_notice_code,
                    dunning_event.occurred_at,
                    dunning_event.collection_case_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT collection_dunning_event_id
                    FROM billing_core.collection_dunning_event
                    WHERE collection_case_id = %s AND dunning_notice_code = %s
                    """,
                    (
                        dunning_event.collection_case_id,
                        dunning_event.dunning_notice_code,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "collection dunning event identity requires a stored case"
                    )
            return self._fetch_collection_dunning_event(cursor, UUID(str(row[0])))

    def find_collection_dispute(
        self, tenant_account_id: UUID, collection_case_id: UUID
    ) -> StoredCollectionDispute | None:
        """Return the dispute-hold row for one tenant collection case, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dispute_id
                FROM billing_core.collection_dispute
                WHERE tenant_account_id = %s AND collection_case_id = %s
                """,
                (tenant_account_id, collection_case_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_dispute(cursor, UUID(str(row[0])))
            )

    def get_collection_dispute(
        self, collection_dispute_id: UUID
    ) -> StoredCollectionDispute | None:
        """Return one collection dispute by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dispute_id
                FROM billing_core.collection_dispute
                WHERE collection_dispute_id = %s
                """,
                (collection_dispute_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_dispute(cursor, UUID(str(row[0])))
            )

    def list_collection_disputes_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionDispute, ...]:
        """Return collection disputes limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dispute_id
                FROM billing_core.collection_dispute
                WHERE tenant_account_id = %s
                ORDER BY held_at, collection_dispute_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_collection_dispute(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_collection_dispute(
        self, collection_dispute: StoredCollectionDispute
    ) -> StoredCollectionDispute:
        """Persist one held commercial dispute or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(collection_dispute.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                collection_dispute.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if collection_dispute.collection_dispute_status != "held":
            raise ValueError("collection_dispute_status must be held")
        remaining = parse_exact_decimal(
            format_exact_decimal(collection_dispute.remaining_outstanding_amount)
        )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.collection_dispute
                    (collection_dispute_id, tenant_account_id, collection_case_id,
                     invoice_draft_id, issued_invoice_id,
                     collection_dispute_contract_version, source_payload_hash,
                     currency_code, remaining_outstanding_amount,
                     collection_dispute_status, held_at, released_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING collection_dispute_id
                """,
                (
                    collection_dispute.collection_dispute_id,
                    collection_dispute.tenant_account_id,
                    collection_dispute.collection_case_id,
                    collection_dispute.invoice_draft_id,
                    collection_dispute.issued_invoice_id,
                    collection_dispute.collection_dispute_contract_version,
                    collection_dispute.source_payload_hash,
                    collection_dispute.currency_code,
                    remaining,
                    collection_dispute.collection_dispute_status,
                    collection_dispute.held_at,
                    collection_dispute.released_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT collection_dispute_id
                    FROM billing_core.collection_dispute
                    WHERE tenant_account_id = %s AND collection_case_id = %s
                    """,
                    (
                        collection_dispute.tenant_account_id,
                        collection_dispute.collection_case_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "collection dispute identity conflicts with an existing row"
                    )
            return self._fetch_collection_dispute(cursor, UUID(str(row[0])))

    def mark_collection_dispute_released(
        self, collection_dispute_id: UUID, released_at: datetime
    ) -> StoredCollectionDispute:
        """Flip one held dispute to ``released`` without changing remaining."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_dispute_id, tenant_account_id, collection_case_id,
                       invoice_draft_id, issued_invoice_id,
                       collection_dispute_contract_version, source_payload_hash,
                       currency_code, remaining_outstanding_amount,
                       collection_dispute_status, held_at, released_at
                FROM billing_core.collection_dispute
                WHERE collection_dispute_id = %s
                FOR UPDATE
                """,
                (collection_dispute_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("collection dispute release requires a stored dispute")
            stored = self._collection_dispute_from_row(row)
            if stored.collection_dispute_status == "released":
                return stored
            if (
                stored.collection_dispute_status != "held"
            ):  # pragma: no cover - held/released only
                raise ValueError("only held collection disputes can release")
            cursor.execute(
                """
                UPDATE billing_core.collection_dispute
                SET collection_dispute_status = 'released', released_at = %s
                WHERE collection_dispute_id = %s
                RETURNING collection_dispute_id
                """,
                (released_at, collection_dispute_id),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError("collection dispute release requires a stored dispute")
            return self._fetch_collection_dispute(cursor, UUID(str(updated[0])))

    def mark_collection_case_disputed(
        self, collection_case_id: UUID
    ) -> StoredCollectionCase:
        """Flip an open or dunning case to ``disputed`` without changing outstanding."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("collection dispute requires a stored collection case")
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status == "disputed":
                return stored
            if stored.collection_case_status not in {"open", "dunning"}:
                raise ValueError(
                    "only open or dunning collection cases can hold as disputed"
                )
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = 'disputed'
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (collection_case_id,),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError("collection dispute requires a stored collection case")
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def mark_collection_case_released_from_dispute(
        self, collection_case_id: UUID
    ) -> StoredCollectionCase:
        """Restore a disputed case to open or dunning without changing outstanding."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "collection dispute release requires a stored collection case"
                )
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status in {"open", "dunning"}:
                return stored
            if stored.collection_case_status == "settled":
                raise ValueError("settled collection cases cannot release from dispute")
            if stored.collection_case_status == "voided":
                raise ValueError("voided collection cases cannot release from dispute")
            if (
                stored.collection_case_status != "disputed"
            ):  # pragma: no cover - closed set
                raise ValueError("only disputed collection cases can release to open")
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.collection_dunning_event
                WHERE collection_case_id = %s
                LIMIT 1
                """,
                (collection_case_id,),
            )
            restored_status = "dunning" if cursor.fetchone() is not None else "open"
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = %s
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (restored_status, collection_case_id),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError(
                    "collection dispute release requires a stored collection case"
                )
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def apply_collection_settlement(
        self, collection_case_id: UUID, applied_amount: Any
    ) -> StoredCollectionCase:
        """Reduce one case balance and settle it when the exact remainder is zero."""
        applied = parse_exact_decimal(format_exact_decimal(applied_amount))
        if applied <= 0:
            raise ValueError(
                "collection settlement amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "collection settlement requires a stored collection case"
                )
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status == "voided":
                raise ValueError(
                    "voided collection cases cannot accept a settlement apply"
                )
            if stored.collection_case_status == "disputed":
                raise ValueError(
                    "disputed collection cases cannot accept a settlement apply"
                )
            if applied > stored.outstanding_amount:
                raise ValueError(
                    "collection settlement amount cannot exceed outstanding"
                )
            remaining = stored.outstanding_amount - applied
            status = "settled" if remaining == 0 else stored.collection_case_status
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = %s, outstanding_amount = %s
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (status, remaining, collection_case_id),
            )
            row = cursor.fetchone()
            if row is None:  # pragma: no cover - the row is locked above
                raise ValueError(
                    "collection settlement requires a stored collection case"
                )
            return self._fetch_collection_case(cursor, UUID(str(row[0])))

    def find_collection_write_off(
        self, tenant_account_id: UUID, collection_case_id: UUID
    ) -> StoredCollectionWriteOff | None:
        """Return one tenant-scoped collection write-off identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_write_off_id
                FROM billing_core.collection_write_off
                WHERE tenant_account_id = %s AND collection_case_id = %s
                """,
                (tenant_account_id, collection_case_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_write_off(cursor, UUID(str(row[0])))
            )

    def get_collection_write_off(
        self, collection_write_off_id: UUID
    ) -> StoredCollectionWriteOff | None:
        """Return one collection write-off by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_write_off_id
                FROM billing_core.collection_write_off
                WHERE collection_write_off_id = %s
                """,
                (collection_write_off_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_write_off(cursor, UUID(str(row[0])))
            )

    def list_collection_write_offs_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionWriteOff, ...]:
        """Return collection write-offs limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_write_off_id
                FROM billing_core.collection_write_off
                WHERE tenant_account_id = %s
                ORDER BY written_off_at, collection_write_off_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_collection_write_off(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_collection_write_off(
        self, write_off: StoredCollectionWriteOff
    ) -> StoredCollectionWriteOff:
        """Persist one exact-zero commercial write-off or replay it."""
        if write_off.collection_write_off_status != "recorded":
            raise ValueError("collection_write_off_status must be recorded")
        write_off_amount = parse_exact_decimal(
            format_exact_decimal(write_off.write_off_amount)
        )
        remaining = parse_exact_decimal(
            format_exact_decimal(write_off.remaining_outstanding_amount)
        )
        if write_off_amount <= 0:
            raise ValueError(
                "collection write-off amount must be a positive exact decimal"
            )
        if remaining != 0:
            raise ValueError("collection write-off remaining must be exact zero")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.collection_write_off
                    (collection_write_off_id, tenant_account_id, collection_case_id,
                     invoice_draft_id, issued_invoice_id,
                     collection_write_off_contract_version, source_payload_hash,
                     currency_code, write_off_amount, remaining_outstanding_amount,
                     collection_write_off_status, written_off_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING collection_write_off_id
                """,
                (
                    write_off.collection_write_off_id,
                    write_off.tenant_account_id,
                    write_off.collection_case_id,
                    write_off.invoice_draft_id,
                    write_off.issued_invoice_id,
                    write_off.collection_write_off_contract_version,
                    write_off.source_payload_hash,
                    write_off.currency_code,
                    write_off_amount,
                    remaining,
                    write_off.collection_write_off_status,
                    write_off.written_off_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT collection_write_off_id
                    FROM billing_core.collection_write_off
                    WHERE tenant_account_id = %s AND collection_case_id = %s
                    """,
                    (write_off.tenant_account_id, write_off.collection_case_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "collection write-off identity conflicts with an existing row"
                    )
            return self._fetch_collection_write_off(cursor, UUID(str(row[0])))

    def apply_collection_write_off(
        self, collection_case_id: UUID, write_off_amount: Any
    ) -> StoredCollectionCase:
        """Zero one open collection case without marking it settled."""
        amount = parse_exact_decimal(format_exact_decimal(write_off_amount))
        if amount <= 0:
            raise ValueError(
                "collection write-off amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "collection write-off requires a stored collection case"
                )
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status in {"settled", "voided", "disputed"}:
                raise ValueError("settled collection cases cannot accept a write-off")
            if amount != stored.outstanding_amount:
                raise ValueError(
                    "collection write-off amount must equal remaining outstanding"
                )
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET outstanding_amount = 0
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (collection_case_id,),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError(
                    "collection write-off requires a stored collection case"
                )
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def find_collection_case_settlement(
        self, tenant_account_id: UUID, collection_case_id: UUID
    ) -> StoredCollectionCaseSettlement | None:
        """Return one tenant-scoped settle-when-zero identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_settlement_id
                FROM billing_core.collection_case_settlement
                WHERE tenant_account_id = %s AND collection_case_id = %s
                """,
                (tenant_account_id, collection_case_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_case_settlement(cursor, UUID(str(row[0])))
            )

    def get_collection_case_settlement(
        self, collection_case_settlement_id: UUID
    ) -> StoredCollectionCaseSettlement | None:
        """Return one collection-case settlement by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_settlement_id
                FROM billing_core.collection_case_settlement
                WHERE collection_case_settlement_id = %s
                """,
                (collection_case_settlement_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_collection_case_settlement(cursor, UUID(str(row[0])))
            )

    def list_collection_case_settlements_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCollectionCaseSettlement, ...]:
        """Return collection-case settlements limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_settlement_id
                FROM billing_core.collection_case_settlement
                WHERE tenant_account_id = %s
                ORDER BY settled_at, collection_case_settlement_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_collection_case_settlement(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_collection_case_settlement(
        self, settlement: StoredCollectionCaseSettlement
    ) -> StoredCollectionCaseSettlement:
        """Persist one exact-zero settlement or return its identity replay."""
        if settlement.collection_case_settlement_status != "settled":
            raise ValueError("collection_case_settlement_status must be settled")
        remaining = parse_exact_decimal(
            format_exact_decimal(settlement.remaining_outstanding_amount)
        )
        if remaining != 0:
            raise ValueError("collection case settlement remaining must be exact zero")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.collection_case_settlement
                    (collection_case_settlement_id, tenant_account_id,
                     collection_case_id, invoice_draft_id, issued_invoice_id,
                     collection_case_settlement_contract_version, source_payload_hash,
                     currency_code, remaining_outstanding_amount,
                     collection_case_settlement_status, settled_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING collection_case_settlement_id
                """,
                (
                    settlement.collection_case_settlement_id,
                    settlement.tenant_account_id,
                    settlement.collection_case_id,
                    settlement.invoice_draft_id,
                    settlement.issued_invoice_id,
                    settlement.collection_case_settlement_contract_version,
                    settlement.source_payload_hash,
                    settlement.currency_code,
                    remaining,
                    settlement.collection_case_settlement_status,
                    settlement.settled_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT collection_case_settlement_id
                    FROM billing_core.collection_case_settlement
                    WHERE tenant_account_id = %s AND collection_case_id = %s
                    """,
                    (settlement.tenant_account_id, settlement.collection_case_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "collection settlement identity conflicts with an existing row"
                    )
            return self._fetch_collection_case_settlement(cursor, UUID(str(row[0])))

    def mark_collection_case_settled(
        self, collection_case_id: UUID
    ) -> StoredCollectionCase:
        """Mark one exact-zero open case settled under a row lock."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "collection settlement requires a stored collection case"
                )
            stored = self._collection_case_from_row(row)
            remaining = parse_exact_decimal(
                format_exact_decimal(stored.outstanding_amount)
            )
            if remaining != 0:
                raise ValueError(
                    "collection case outstanding must be exact zero to settle"
                )
            if stored.collection_case_status == "settled":
                return stored
            if stored.collection_case_status == "voided":
                raise ValueError("voided collection cases cannot settle")
            if stored.collection_case_status == "disputed":
                raise ValueError("disputed collection cases cannot settle")
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = 'settled'
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (collection_case_id,),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError(
                    "collection settlement requires a stored collection case"
                )
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def mark_collection_case_voided(
        self, collection_case_id: UUID, expected_outstanding: Any
    ) -> StoredCollectionCase:
        """Close an unused open or dunning case as ``voided`` at exact zero."""
        expected = parse_exact_decimal(format_exact_decimal(expected_outstanding))
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("collection void requires a stored collection case")
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status == "voided":
                return stored
            if stored.collection_case_status not in {"open", "dunning"}:
                raise ValueError("only open or dunning collection cases can void")
            remaining = parse_exact_decimal(
                format_exact_decimal(stored.outstanding_amount)
            )
            if remaining != expected:
                raise ValueError(
                    "collection void remaining must equal the issued amount"
                )
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET collection_case_status = 'voided', outstanding_amount = 0
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (collection_case_id,),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError("collection void requires a stored collection case")
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def get_payment_intent(self, payment_intent_id: UUID) -> StoredPaymentIntent | None:
        """Return one payment intent by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_intent_id
                FROM billing_core.payment_intent
                WHERE payment_intent_id = %s
                """,
                (payment_intent_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_payment_intent(cursor, UUID(str(row[0])))
            )

    def find_payment_intent(
        self,
        tenant_account_id: UUID,
        collection_case_id: UUID,
        source_payload_hash: str,
        payment_intent_contract_version: int,
    ) -> StoredPaymentIntent | None:
        """Return one tenant-scoped payment-intent identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_intent_id
                FROM billing_core.payment_intent
                WHERE tenant_account_id = %s
                  AND collection_case_id = %s
                  AND source_payload_hash = %s
                  AND payment_intent_contract_version = %s
                """,
                (
                    tenant_account_id,
                    collection_case_id,
                    source_payload_hash,
                    payment_intent_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_payment_intent(cursor, UUID(str(row[0])))
            )

    def insert_payment_intent(
        self, payment_intent: StoredPaymentIntent
    ) -> StoredPaymentIntent:
        """Persist one positive provider-neutral intent or replay it."""
        if payment_intent.payment_intent_status not in {
            "projected",
            "cancelled",
            "rejected",
        }:
            raise ValueError("payment intents cannot be captured, settled, or posted")
        payment_amount = parse_exact_decimal(
            format_exact_decimal(payment_intent.payment_amount)
        )
        if payment_amount <= 0:
            raise ValueError("payment intent amount must be a positive exact decimal")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.payment_intent
                    (payment_intent_id, tenant_account_id, collection_case_id,
                     payment_intent_contract_version, currency_code,
                     payment_intent_status, payment_amount, source_payload_hash,
                     projected_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING payment_intent_id
                """,
                (
                    payment_intent.payment_intent_id,
                    payment_intent.tenant_account_id,
                    payment_intent.collection_case_id,
                    payment_intent.payment_intent_contract_version,
                    payment_intent.currency_code,
                    payment_intent.payment_intent_status,
                    payment_amount,
                    payment_intent.source_payload_hash,
                    payment_intent.projected_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT payment_intent_id
                    FROM billing_core.payment_intent
                    WHERE tenant_account_id = %s
                      AND collection_case_id = %s
                      AND source_payload_hash = %s
                      AND payment_intent_contract_version = %s
                    """,
                    (
                        payment_intent.tenant_account_id,
                        payment_intent.collection_case_id,
                        payment_intent.source_payload_hash,
                        payment_intent.payment_intent_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if (
                    row is None
                ):  # pragma: no cover - a valid FK conflict has an identity row
                    raise ValueError(
                        "payment intent identity already belongs to another intent"
                    )
            return self._fetch_payment_intent(cursor, UUID(str(row[0])))

    def list_payment_intents(
        self, tenant_account_id: UUID
    ) -> tuple[StoredPaymentIntent, ...]:
        """Return payment intents limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_intent_id
                FROM billing_core.payment_intent
                WHERE tenant_account_id = %s
                ORDER BY projected_at, payment_intent_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_payment_intent(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def cancel_stored_payment_intent(
        self, payment_intent_id: UUID
    ) -> StoredPaymentIntent:
        """Cancel one projected intent idempotently."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE billing_core.payment_intent
                SET payment_intent_status = 'cancelled'
                WHERE payment_intent_id = %s
                  AND payment_intent_status = 'projected'
                RETURNING payment_intent_id
                """,
                (payment_intent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT payment_intent_id, payment_intent_status
                    FROM billing_core.payment_intent
                    WHERE payment_intent_id = %s
                    """,
                    (payment_intent_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "payment intent cancellation requires a stored payment intent"
                    )
                if row[1] != "cancelled":
                    raise ValueError("only projected payment intents can be cancelled")
            return self._fetch_payment_intent(cursor, UUID(str(row[0])))

    def get_payment_receipt(
        self, payment_receipt_id: UUID
    ) -> StoredPaymentReceipt | None:
        """Return one payment receipt by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_receipt_id
                FROM billing_core.payment_receipt
                WHERE payment_receipt_id = %s
                """,
                (payment_receipt_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_payment_receipt(cursor, UUID(str(row[0])))
            )

    def find_payment_receipt(
        self,
        tenant_account_id: UUID,
        payment_intent_id: UUID,
        source_payload_hash: str,
        settlement_contract_version: int,
    ) -> StoredPaymentReceipt | None:
        """Return one tenant-scoped payment-receipt identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_receipt_id
                FROM billing_core.payment_receipt
                WHERE tenant_account_id = %s
                  AND payment_intent_id = %s
                  AND source_payload_hash = %s
                  AND settlement_contract_version = %s
                """,
                (
                    tenant_account_id,
                    payment_intent_id,
                    source_payload_hash,
                    settlement_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_payment_receipt(cursor, UUID(str(row[0])))
            )

    def insert_payment_receipt(
        self, payment_receipt: StoredPaymentReceipt
    ) -> StoredPaymentReceipt:
        """Persist one applied receipt or return the exact identity replay."""
        if payment_receipt.payment_receipt_status != "applied":
            raise ValueError("payment receipts cannot be captured or posted")
        received_amount = parse_exact_decimal(
            format_exact_decimal(payment_receipt.received_amount)
        )
        if received_amount <= 0:
            raise ValueError("payment receipt amount must be a positive exact decimal")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.payment_receipt
                    (payment_receipt_id, tenant_account_id, payment_intent_id,
                     collection_case_id, settlement_contract_version, currency_code,
                     payment_receipt_status, received_amount, source_payload_hash,
                     received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING payment_receipt_id
                """,
                (
                    payment_receipt.payment_receipt_id,
                    payment_receipt.tenant_account_id,
                    payment_receipt.payment_intent_id,
                    payment_receipt.collection_case_id,
                    payment_receipt.settlement_contract_version,
                    payment_receipt.currency_code,
                    payment_receipt.payment_receipt_status,
                    received_amount,
                    payment_receipt.source_payload_hash,
                    payment_receipt.received_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT payment_receipt_id
                    FROM billing_core.payment_receipt
                    WHERE tenant_account_id = %s
                      AND payment_intent_id = %s
                      AND source_payload_hash = %s
                      AND settlement_contract_version = %s
                    """,
                    (
                        payment_receipt.tenant_account_id,
                        payment_receipt.payment_intent_id,
                        payment_receipt.source_payload_hash,
                        payment_receipt.settlement_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "payment receipt identity conflicts with an existing row"
                    )
            return self._fetch_payment_receipt(cursor, UUID(str(row[0])))

    def list_payment_receipts(
        self, tenant_account_id: UUID
    ) -> tuple[StoredPaymentReceipt, ...]:
        """Return payment receipts limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payment_receipt_id
                FROM billing_core.payment_receipt
                WHERE tenant_account_id = %s
                ORDER BY received_at, payment_receipt_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_payment_receipt(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def find_unapplied_cash(
        self, tenant_account_id: UUID, payment_receipt_id: UUID
    ) -> StoredUnappliedCash | None:
        """Return the parked leftover for one tenant payment receipt, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_id
                FROM billing_core.unapplied_cash
                WHERE tenant_account_id = %s AND payment_receipt_id = %s
                """,
                (tenant_account_id, payment_receipt_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash(cursor, UUID(str(row[0])))
            )

    def get_unapplied_cash(self, unapplied_cash_id: UUID) -> StoredUnappliedCash | None:
        """Return one unapplied-cash row by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_id
                FROM billing_core.unapplied_cash
                WHERE unapplied_cash_id = %s
                """,
                (unapplied_cash_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash(cursor, UUID(str(row[0])))
            )

    def list_unapplied_cash_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCash, ...]:
        """Return unapplied-cash rows limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_id
                FROM billing_core.unapplied_cash
                WHERE tenant_account_id = %s
                ORDER BY parked_at, unapplied_cash_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_unapplied_cash(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_unapplied_cash(
        self, unapplied_cash: StoredUnappliedCash
    ) -> StoredUnappliedCash:
        """Persist one parked leftover or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(unapplied_cash.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(unapplied_cash.source_payload_hash)
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if unapplied_cash.unapplied_cash_status != "parked":
            raise ValueError("unapplied_cash_status must be parked")
        leftover = parse_exact_decimal(
            format_exact_decimal(unapplied_cash.unapplied_amount)
        )
        if leftover <= 0:
            raise ValueError("unapplied cash amount must be a positive exact decimal")
        received_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash.received_amount)
        )
        applied_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash.applied_amount)
        )
        if received_amount <= 0:
            raise ValueError(
                "unapplied cash received amount must be a positive exact decimal"
            )
        if applied_amount <= 0:
            raise ValueError(
                "unapplied cash applied amount must be a positive exact decimal"
            )
        if leftover > received_amount:
            raise ValueError("unapplied cash cannot exceed the stored receipt")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.unapplied_cash
                    (unapplied_cash_id, tenant_account_id, payment_receipt_id,
                     payment_intent_id, collection_case_id,
                     unapplied_cash_contract_version, source_payload_hash,
                     currency_code, unapplied_amount, received_amount,
                     applied_amount, unapplied_cash_status, parked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING unapplied_cash_id
                """,
                (
                    unapplied_cash.unapplied_cash_id,
                    unapplied_cash.tenant_account_id,
                    unapplied_cash.payment_receipt_id,
                    unapplied_cash.payment_intent_id,
                    unapplied_cash.collection_case_id,
                    unapplied_cash.unapplied_cash_contract_version,
                    unapplied_cash.source_payload_hash,
                    unapplied_cash.currency_code,
                    leftover,
                    received_amount,
                    applied_amount,
                    unapplied_cash.unapplied_cash_status,
                    unapplied_cash.parked_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT unapplied_cash_id
                    FROM billing_core.unapplied_cash
                    WHERE tenant_account_id = %s AND payment_receipt_id = %s
                    """,
                    (
                        unapplied_cash.tenant_account_id,
                        unapplied_cash.payment_receipt_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "unapplied cash identity conflicts with an existing row"
                    )
            return self._fetch_unapplied_cash(cursor, UUID(str(row[0])))

    def find_unapplied_cash_application(
        self, tenant_account_id: UUID, unapplied_cash_id: UUID
    ) -> StoredUnappliedCashApplication | None:
        """Return the leftover apply for one tenant parked leftover, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_application_id
                FROM billing_core.unapplied_cash_application
                WHERE tenant_account_id = %s AND unapplied_cash_id = %s
                """,
                (tenant_account_id, unapplied_cash_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash_application(cursor, UUID(str(row[0])))
            )

    def get_unapplied_cash_application(
        self, unapplied_cash_application_id: UUID
    ) -> StoredUnappliedCashApplication | None:
        """Return one leftover application by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_application_id
                FROM billing_core.unapplied_cash_application
                WHERE unapplied_cash_application_id = %s
                """,
                (unapplied_cash_application_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash_application(cursor, UUID(str(row[0])))
            )

    def list_unapplied_cash_applications_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCashApplication, ...]:
        """Return leftover applications limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_application_id
                FROM billing_core.unapplied_cash_application
                WHERE tenant_account_id = %s
                ORDER BY applied_at, unapplied_cash_application_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_unapplied_cash_application(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_unapplied_cash_application(
        self, unapplied_cash_application: StoredUnappliedCashApplication
    ) -> StoredUnappliedCashApplication:
        """Persist one leftover apply or return its identity replay."""
        if (
            CURRENCY_CODE_PATTERN.fullmatch(unapplied_cash_application.currency_code)
            is None
        ):
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                unapplied_cash_application.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if unapplied_cash_application.unapplied_cash_application_status != "applied":
            raise ValueError("unapplied_cash_application_status must be applied")
        applied_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_application.applied_amount)
        )
        if applied_amount <= 0:
            raise ValueError(
                "unapplied cash application amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.unapplied_cash_application
                    (unapplied_cash_application_id, tenant_account_id,
                     unapplied_cash_id, collection_case_id, payment_receipt_id,
                     invoice_draft_id, unapplied_cash_application_contract_version,
                     source_payload_hash, currency_code, applied_amount,
                     unapplied_cash_application_status, applied_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING unapplied_cash_application_id
                """,
                (
                    unapplied_cash_application.unapplied_cash_application_id,
                    unapplied_cash_application.tenant_account_id,
                    unapplied_cash_application.unapplied_cash_id,
                    unapplied_cash_application.collection_case_id,
                    unapplied_cash_application.payment_receipt_id,
                    unapplied_cash_application.invoice_draft_id,
                    unapplied_cash_application.unapplied_cash_application_contract_version,
                    unapplied_cash_application.source_payload_hash,
                    unapplied_cash_application.currency_code,
                    applied_amount,
                    unapplied_cash_application.unapplied_cash_application_status,
                    unapplied_cash_application.applied_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT unapplied_cash_application_id
                    FROM billing_core.unapplied_cash_application
                    WHERE tenant_account_id = %s AND unapplied_cash_id = %s
                    """,
                    (
                        unapplied_cash_application.tenant_account_id,
                        unapplied_cash_application.unapplied_cash_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "unapplied cash application identity conflicts with an existing row"
                    )
            return self._fetch_unapplied_cash_application(cursor, UUID(str(row[0])))

    def find_unapplied_cash_refund(
        self, tenant_account_id: UUID, unapplied_cash_id: UUID
    ) -> StoredUnappliedCashRefund | None:
        """Return the leftover refund for one tenant parked leftover, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_refund_id
                FROM billing_core.unapplied_cash_refund
                WHERE tenant_account_id = %s AND unapplied_cash_id = %s
                """,
                (tenant_account_id, unapplied_cash_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash_refund(cursor, UUID(str(row[0])))
            )

    def get_unapplied_cash_refund(
        self, unapplied_cash_refund_id: UUID
    ) -> StoredUnappliedCashRefund | None:
        """Return one leftover refund by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_refund_id
                FROM billing_core.unapplied_cash_refund
                WHERE unapplied_cash_refund_id = %s
                """,
                (unapplied_cash_refund_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_unapplied_cash_refund(cursor, UUID(str(row[0])))
            )

    def list_unapplied_cash_refunds_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredUnappliedCashRefund, ...]:
        """Return leftover refunds limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT unapplied_cash_refund_id
                FROM billing_core.unapplied_cash_refund
                WHERE tenant_account_id = %s
                ORDER BY refunded_at, unapplied_cash_refund_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_unapplied_cash_refund(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_unapplied_cash_refund(
        self, unapplied_cash_refund: StoredUnappliedCashRefund
    ) -> StoredUnappliedCashRefund:
        """Persist one leftover refund or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(unapplied_cash_refund.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                unapplied_cash_refund.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if unapplied_cash_refund.unapplied_cash_refund_status != "recorded":
            raise ValueError("unapplied_cash_refund_status must be recorded")
        refund_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_refund.refund_amount)
        )
        if refund_amount <= 0:
            raise ValueError(
                "unapplied cash refund amount must be a positive exact decimal"
            )
        unapplied_amount = parse_exact_decimal(
            format_exact_decimal(unapplied_cash_refund.unapplied_amount)
        )
        if unapplied_amount <= 0:
            raise ValueError("unapplied cash amount must be a positive exact decimal")
        if refund_amount != unapplied_amount:
            raise ValueError(
                "unapplied cash refund amount must equal the parked leftover"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.unapplied_cash_refund
                    (unapplied_cash_refund_id, tenant_account_id, unapplied_cash_id,
                     payment_receipt_id, payment_intent_id, collection_case_id,
                     unapplied_cash_refund_contract_version, source_payload_hash,
                     currency_code, refund_amount, unapplied_amount,
                     unapplied_cash_refund_status, refunded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING unapplied_cash_refund_id
                """,
                (
                    unapplied_cash_refund.unapplied_cash_refund_id,
                    unapplied_cash_refund.tenant_account_id,
                    unapplied_cash_refund.unapplied_cash_id,
                    unapplied_cash_refund.payment_receipt_id,
                    unapplied_cash_refund.payment_intent_id,
                    unapplied_cash_refund.collection_case_id,
                    unapplied_cash_refund.unapplied_cash_refund_contract_version,
                    unapplied_cash_refund.source_payload_hash,
                    unapplied_cash_refund.currency_code,
                    refund_amount,
                    unapplied_amount,
                    unapplied_cash_refund.unapplied_cash_refund_status,
                    unapplied_cash_refund.refunded_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT unapplied_cash_refund_id
                    FROM billing_core.unapplied_cash_refund
                    WHERE tenant_account_id = %s AND unapplied_cash_id = %s
                    """,
                    (
                        unapplied_cash_refund.tenant_account_id,
                        unapplied_cash_refund.unapplied_cash_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "unapplied cash refund identity conflicts with an existing row"
                    )
            return self._fetch_unapplied_cash_refund(cursor, UUID(str(row[0])))

    def apply_unapplied_cash_to_collection_case(
        self, collection_case_id: UUID, applied_amount: Any
    ) -> StoredCollectionCase:
        """Reduce outstanding by parked leftover without flipping to settled.

        #46 remains the explicit settle-when-zero command. Status stays
        ``open`` or ``dunning`` even when remaining becomes exact zero.
        """
        amount = parse_exact_decimal(format_exact_decimal(applied_amount))
        if amount <= 0:
            raise ValueError(
                "unapplied cash apply amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                       currency_code, collection_case_status, outstanding_amount, opened_at
                FROM billing_core.collection_case
                WHERE collection_case_id = %s
                FOR UPDATE
                """,
                (collection_case_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(
                    "unapplied cash apply requires a stored collection case"
                )
            stored = self._collection_case_from_row(row)
            if stored.collection_case_status in {"settled", "voided", "disputed"}:
                raise ValueError(
                    "settled collection cases cannot accept unapplied cash"
                )
            remaining = parse_exact_decimal(
                format_exact_decimal(stored.outstanding_amount)
            )
            if amount > remaining:
                raise ValueError(
                    "unapplied cash apply amount cannot exceed outstanding"
                )
            cursor.execute(
                """
                UPDATE billing_core.collection_case
                SET outstanding_amount = %s
                WHERE collection_case_id = %s
                RETURNING collection_case_id
                """,
                (remaining - amount, collection_case_id),
            )
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - the row is locked above
                raise ValueError(
                    "unapplied cash apply requires a stored collection case"
                )
            return self._fetch_collection_case(cursor, UUID(str(updated[0])))

    def get_credit_adjustment(
        self, credit_adjustment_id: UUID
    ) -> StoredCreditAdjustment | None:
        """Return one credit adjustment by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_adjustment_id
                FROM billing_core.credit_adjustment
                WHERE credit_adjustment_id = %s
                """,
                (credit_adjustment_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_credit_adjustment(cursor, UUID(str(row[0])))
            )

    def find_credit_adjustment(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        source_payload_hash: str,
        credit_adjustment_contract_version: int,
    ) -> StoredCreditAdjustment | None:
        """Return one tenant-scoped credit-adjustment identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_adjustment_id
                FROM billing_core.credit_adjustment
                WHERE tenant_account_id = %s
                  AND invoice_draft_id = %s
                  AND source_payload_hash = %s
                  AND credit_adjustment_contract_version = %s
                """,
                (
                    tenant_account_id,
                    invoice_draft_id,
                    source_payload_hash,
                    credit_adjustment_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_credit_adjustment(cursor, UUID(str(row[0])))
            )

    def insert_credit_adjustment(
        self, credit: StoredCreditAdjustment
    ) -> StoredCreditAdjustment:
        """Persist one exact credit adjustment or return its identity replay."""
        if credit.credit_reason_code not in {
            "rating_correction",
            "goodwill",
            "billing_error",
        }:
            raise ValueError("credit_reason_code is not in the closed set")
        credit_amount = parse_exact_decimal(format_exact_decimal(credit.credit_amount))
        tax_exclusive_amount = parse_exact_decimal(
            format_exact_decimal(credit.tax_exclusive_amount)
        )
        tax_amount = parse_exact_decimal(format_exact_decimal(credit.tax_amount))
        if credit_amount <= 0:
            raise ValueError("credit amount must be a positive exact decimal")
        if tax_exclusive_amount + tax_amount != credit_amount:
            raise ValueError("credit tax split must sum to credit_amount")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.credit_adjustment
                    (credit_adjustment_id, tenant_account_id, invoice_draft_id,
                     credit_adjustment_contract_version, credit_reason_code,
                     currency_code, credit_amount, tax_exclusive_amount, tax_amount,
                     source_payload_hash, recorded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING credit_adjustment_id
                """,
                (
                    credit.credit_adjustment_id,
                    credit.tenant_account_id,
                    credit.invoice_draft_id,
                    credit.credit_adjustment_contract_version,
                    credit.credit_reason_code,
                    credit.currency_code,
                    credit_amount,
                    tax_exclusive_amount,
                    tax_amount,
                    credit.source_payload_hash,
                    credit.recorded_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT credit_adjustment_id
                    FROM billing_core.credit_adjustment
                    WHERE tenant_account_id = %s
                      AND invoice_draft_id = %s
                      AND source_payload_hash = %s
                      AND credit_adjustment_contract_version = %s
                    """,
                    (
                        credit.tenant_account_id,
                        credit.invoice_draft_id,
                        credit.source_payload_hash,
                        credit.credit_adjustment_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "credit adjustment identity conflicts with an existing row"
                    )
            return self._fetch_credit_adjustment(cursor, UUID(str(row[0])))

    def list_credit_adjustments(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCreditAdjustment, ...]:
        """Return credit adjustments limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_adjustment_id
                FROM billing_core.credit_adjustment
                WHERE tenant_account_id = %s
                ORDER BY recorded_at, credit_adjustment_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_credit_adjustment(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def find_issued_credit_note(
        self, tenant_account_id: UUID, credit_adjustment_id: UUID
    ) -> StoredIssuedCreditNote | None:
        """Return the issued credit note for one tenant credit, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_id
                FROM billing_core.issued_credit_note
                WHERE tenant_account_id = %s AND credit_adjustment_id = %s
                """,
                (tenant_account_id, credit_adjustment_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_credit_note(cursor, UUID(str(row[0])))
            )

    def get_issued_credit_note(
        self, issued_credit_note_id: UUID
    ) -> StoredIssuedCreditNote | None:
        """Return one issued credit note by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_id
                FROM billing_core.issued_credit_note
                WHERE issued_credit_note_id = %s
                """,
                (issued_credit_note_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_credit_note(cursor, UUID(str(row[0])))
            )

    def list_issued_credit_notes_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedCreditNote, ...]:
        """Return issued credit notes limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_id
                FROM billing_core.issued_credit_note
                WHERE tenant_account_id = %s
                ORDER BY issued_at, issued_credit_note_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_issued_credit_note(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_issued_credit_note(
        self, issued_credit_note: StoredIssuedCreditNote
    ) -> StoredIssuedCreditNote:
        """Persist one commercial credit-note snapshot or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(issued_credit_note.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                issued_credit_note.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                issued_credit_note.credit_adjustment_source_payload_hash
            )
            is None
        ):
            raise ValueError(
                "credit_adjustment_source_payload_hash must be a sha256 digest"
            )
        if issued_credit_note.issued_credit_note_status != "issued":
            raise ValueError("issued_credit_note_status must be issued")
        if issued_credit_note.credit_reason_code not in {
            "rating_correction",
            "goodwill",
            "billing_error",
        }:
            raise ValueError("credit_reason_code is not in the closed set")
        exclusive = parse_exact_decimal(
            format_exact_decimal(issued_credit_note.tax_exclusive_amount)
        )
        tax_amount = parse_exact_decimal(
            format_exact_decimal(issued_credit_note.tax_amount)
        )
        inclusive = parse_exact_decimal(
            format_exact_decimal(issued_credit_note.tax_inclusive_amount)
        )
        if exclusive + tax_amount != inclusive:
            raise ValueError("issued credit note totals must sum")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.issued_credit_note
                    (issued_credit_note_id, tenant_account_id, credit_adjustment_id,
                     invoice_draft_id, issued_invoice_id,
                     issued_credit_note_contract_version,
                     credit_adjustment_contract_version, credit_reason_code,
                     credit_adjustment_source_payload_hash, source_payload_hash,
                     currency_code, tax_exclusive_amount, tax_amount,
                     tax_inclusive_amount, issued_credit_note_status, issued_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING issued_credit_note_id
                """,
                (
                    issued_credit_note.issued_credit_note_id,
                    issued_credit_note.tenant_account_id,
                    issued_credit_note.credit_adjustment_id,
                    issued_credit_note.invoice_draft_id,
                    issued_credit_note.issued_invoice_id,
                    issued_credit_note.issued_credit_note_contract_version,
                    issued_credit_note.credit_adjustment_contract_version,
                    issued_credit_note.credit_reason_code,
                    issued_credit_note.credit_adjustment_source_payload_hash,
                    issued_credit_note.source_payload_hash,
                    issued_credit_note.currency_code,
                    exclusive,
                    tax_amount,
                    inclusive,
                    issued_credit_note.issued_credit_note_status,
                    issued_credit_note.issued_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT issued_credit_note_id
                    FROM billing_core.issued_credit_note
                    WHERE tenant_account_id = %s AND credit_adjustment_id = %s
                    """,
                    (
                        issued_credit_note.tenant_account_id,
                        issued_credit_note.credit_adjustment_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "issued credit note identity conflicts with an existing row"
                    )
            return self._fetch_issued_credit_note(cursor, UUID(str(row[0])))

    def find_issued_invoice_void(
        self, tenant_account_id: UUID, issued_invoice_id: UUID
    ) -> StoredIssuedInvoiceVoid | None:
        """Return the void row for one tenant issued invoice, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_void_id
                FROM billing_core.issued_invoice_void
                WHERE tenant_account_id = %s AND issued_invoice_id = %s
                """,
                (tenant_account_id, issued_invoice_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_invoice_void(cursor, UUID(str(row[0])))
            )

    def get_issued_invoice_void(
        self, issued_invoice_void_id: UUID
    ) -> StoredIssuedInvoiceVoid | None:
        """Return one issued-invoice void by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_void_id
                FROM billing_core.issued_invoice_void
                WHERE issued_invoice_void_id = %s
                """,
                (issued_invoice_void_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_invoice_void(cursor, UUID(str(row[0])))
            )

    def list_issued_invoice_voids_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedInvoiceVoid, ...]:
        """Return issued-invoice voids limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_invoice_void_id
                FROM billing_core.issued_invoice_void
                WHERE tenant_account_id = %s
                ORDER BY voided_at, issued_invoice_void_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_issued_invoice_void(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_issued_invoice_void(
        self, issued_invoice_void: StoredIssuedInvoiceVoid
    ) -> StoredIssuedInvoiceVoid:
        """Persist one unused issued-invoice void or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(issued_invoice_void.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                issued_invoice_void.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if issued_invoice_void.issued_invoice_void_status != "recorded":
            raise ValueError("issued_invoice_void_status must be recorded")
        remaining = parse_exact_decimal(
            format_exact_decimal(issued_invoice_void.remaining_outstanding_amount)
        )
        if remaining != 0:
            raise ValueError("issued-invoice void remaining must be exact zero")
        voided_amount = parse_exact_decimal(
            format_exact_decimal(issued_invoice_void.voided_amount)
        )
        if voided_amount <= 0:
            raise ValueError(
                "issued-invoice void amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.issued_invoice_void
                    (issued_invoice_void_id, tenant_account_id, issued_invoice_id,
                     invoice_draft_id, collection_case_id,
                     issued_invoice_void_contract_version, source_payload_hash,
                     currency_code, voided_amount, remaining_outstanding_amount,
                     issued_invoice_void_status, voided_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING issued_invoice_void_id
                """,
                (
                    issued_invoice_void.issued_invoice_void_id,
                    issued_invoice_void.tenant_account_id,
                    issued_invoice_void.issued_invoice_id,
                    issued_invoice_void.invoice_draft_id,
                    issued_invoice_void.collection_case_id,
                    issued_invoice_void.issued_invoice_void_contract_version,
                    issued_invoice_void.source_payload_hash,
                    issued_invoice_void.currency_code,
                    voided_amount,
                    remaining,
                    issued_invoice_void.issued_invoice_void_status,
                    issued_invoice_void.voided_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT issued_invoice_void_id
                    FROM billing_core.issued_invoice_void
                    WHERE tenant_account_id = %s AND issued_invoice_id = %s
                    """,
                    (
                        issued_invoice_void.tenant_account_id,
                        issued_invoice_void.issued_invoice_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "issued-invoice void identity conflicts with an existing row"
                    )
            return self._fetch_issued_invoice_void(cursor, UUID(str(row[0])))

    def find_issued_credit_note_void(
        self, tenant_account_id: UUID, issued_credit_note_id: UUID
    ) -> StoredIssuedCreditNoteVoid | None:
        """Return the void row for one tenant issued credit note, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_void_id
                FROM billing_core.issued_credit_note_void
                WHERE tenant_account_id = %s AND issued_credit_note_id = %s
                """,
                (tenant_account_id, issued_credit_note_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_credit_note_void(cursor, UUID(str(row[0])))
            )

    def get_issued_credit_note_void(
        self, issued_credit_note_void_id: UUID
    ) -> StoredIssuedCreditNoteVoid | None:
        """Return one issued-credit-note void by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_void_id
                FROM billing_core.issued_credit_note_void
                WHERE issued_credit_note_void_id = %s
                """,
                (issued_credit_note_void_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_issued_credit_note_void(cursor, UUID(str(row[0])))
            )

    def list_issued_credit_note_voids_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredIssuedCreditNoteVoid, ...]:
        """Return issued-credit-note voids limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT issued_credit_note_void_id
                FROM billing_core.issued_credit_note_void
                WHERE tenant_account_id = %s
                ORDER BY voided_at, issued_credit_note_void_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_issued_credit_note_void(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_issued_credit_note_void(
        self, issued_credit_note_void: StoredIssuedCreditNoteVoid
    ) -> StoredIssuedCreditNoteVoid:
        """Persist one unused issued-credit-note void or return its identity replay."""
        if (
            CURRENCY_CODE_PATTERN.fullmatch(issued_credit_note_void.currency_code)
            is None
        ):
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                issued_credit_note_void.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if issued_credit_note_void.issued_credit_note_void_status != "recorded":
            raise ValueError("issued_credit_note_void_status must be recorded")
        voided_amount = parse_exact_decimal(
            format_exact_decimal(issued_credit_note_void.voided_amount)
        )
        if voided_amount <= 0:
            raise ValueError(
                "issued-credit-note void amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.issued_credit_note_void
                    (issued_credit_note_void_id, tenant_account_id,
                     issued_credit_note_id, credit_adjustment_id, invoice_draft_id,
                     issued_invoice_id, issued_credit_note_void_contract_version,
                     source_payload_hash, currency_code, voided_amount,
                     issued_credit_note_void_status, voided_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING issued_credit_note_void_id
                """,
                (
                    issued_credit_note_void.issued_credit_note_void_id,
                    issued_credit_note_void.tenant_account_id,
                    issued_credit_note_void.issued_credit_note_id,
                    issued_credit_note_void.credit_adjustment_id,
                    issued_credit_note_void.invoice_draft_id,
                    issued_credit_note_void.issued_invoice_id,
                    issued_credit_note_void.issued_credit_note_void_contract_version,
                    issued_credit_note_void.source_payload_hash,
                    issued_credit_note_void.currency_code,
                    voided_amount,
                    issued_credit_note_void.issued_credit_note_void_status,
                    issued_credit_note_void.voided_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT issued_credit_note_void_id
                    FROM billing_core.issued_credit_note_void
                    WHERE tenant_account_id = %s AND issued_credit_note_id = %s
                    """,
                    (
                        issued_credit_note_void.tenant_account_id,
                        issued_credit_note_void.issued_credit_note_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "issued-credit-note void identity conflicts with an existing row"
                    )
            return self._fetch_issued_credit_note_void(cursor, UUID(str(row[0])))

    def find_credit_note_application(
        self, tenant_account_id: UUID, issued_credit_note_id: UUID
    ) -> StoredCreditNoteApplication | None:
        """Return the application for one tenant issued credit note, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_note_application_id
                FROM billing_core.credit_note_application
                WHERE tenant_account_id = %s AND issued_credit_note_id = %s
                """,
                (tenant_account_id, issued_credit_note_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_credit_note_application(cursor, UUID(str(row[0])))
            )

    def get_credit_note_application(
        self, credit_note_application_id: UUID
    ) -> StoredCreditNoteApplication | None:
        """Return one credit-note application by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_note_application_id
                FROM billing_core.credit_note_application
                WHERE credit_note_application_id = %s
                """,
                (credit_note_application_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_credit_note_application(cursor, UUID(str(row[0])))
            )

    def list_credit_note_applications_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredCreditNoteApplication, ...]:
        """Return credit-note applications limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT credit_note_application_id
                FROM billing_core.credit_note_application
                WHERE tenant_account_id = %s
                ORDER BY applied_at, credit_note_application_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_credit_note_application(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_credit_note_application(
        self, credit_note_application: StoredCreditNoteApplication
    ) -> StoredCreditNoteApplication:
        """Persist one applied credit-note application or return its identity replay."""
        if (
            CURRENCY_CODE_PATTERN.fullmatch(credit_note_application.currency_code)
            is None
        ):
            raise ValueError("currency_code must be a three-letter ISO code")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                credit_note_application.source_payload_hash
            )
            is None
        ):
            raise ValueError("source_payload_hash must be a sha256 digest")
        if (
            SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(
                credit_note_application.issued_credit_note_source_payload_hash
            )
            is None
        ):
            raise ValueError(
                "issued_credit_note_source_payload_hash must be a sha256 digest"
            )
        if credit_note_application.credit_note_application_status != "applied":
            raise ValueError("credit_note_application_status must be applied")
        applied_amount = parse_exact_decimal(
            format_exact_decimal(credit_note_application.applied_amount)
        )
        if applied_amount <= 0:
            raise ValueError(
                "credit note application amount must be a positive exact decimal"
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.credit_note_application
                    (credit_note_application_id, tenant_account_id,
                     issued_credit_note_id, collection_case_id, invoice_draft_id,
                     issued_invoice_id, credit_note_application_contract_version,
                     issued_credit_note_contract_version, source_payload_hash,
                     issued_credit_note_source_payload_hash, currency_code,
                     applied_amount, credit_note_application_status, applied_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING credit_note_application_id
                """,
                (
                    credit_note_application.credit_note_application_id,
                    credit_note_application.tenant_account_id,
                    credit_note_application.issued_credit_note_id,
                    credit_note_application.collection_case_id,
                    credit_note_application.invoice_draft_id,
                    credit_note_application.issued_invoice_id,
                    credit_note_application.credit_note_application_contract_version,
                    credit_note_application.issued_credit_note_contract_version,
                    credit_note_application.source_payload_hash,
                    credit_note_application.issued_credit_note_source_payload_hash,
                    credit_note_application.currency_code,
                    applied_amount,
                    credit_note_application.credit_note_application_status,
                    credit_note_application.applied_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT credit_note_application_id
                    FROM billing_core.credit_note_application
                    WHERE tenant_account_id = %s AND issued_credit_note_id = %s
                    """,
                    (
                        credit_note_application.tenant_account_id,
                        credit_note_application.issued_credit_note_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "credit-note application identity conflicts with an existing row"
                    )
            return self._fetch_credit_note_application(cursor, UUID(str(row[0])))

    def find_spend_budget(
        self,
        tenant_account_id: UUID,
        billing_account_id: UUID,
        window_started_at: datetime,
        window_ended_at: datetime,
        currency_code: str,
        source_payload_hash: str,
        spend_budget_contract_version: int,
    ) -> StoredSpendBudget | None:
        """Return the spend budget for one tenant-scoped identity, if any."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT spend_budget_id
                FROM billing_core.spend_budget
                WHERE tenant_account_id = %s
                  AND billing_account_id = %s
                  AND window_started_at = %s
                  AND window_ended_at = %s
                  AND currency_code = %s
                  AND source_payload_hash = %s
                  AND spend_budget_contract_version = %s
                """,
                (
                    tenant_account_id,
                    billing_account_id,
                    window_started_at,
                    window_ended_at,
                    currency_code,
                    source_payload_hash,
                    spend_budget_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_spend_budget(cursor, UUID(str(row[0])))
            )

    def get_spend_budget(self, spend_budget_id: UUID) -> StoredSpendBudget | None:
        """Return one spend budget by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT spend_budget_id
                FROM billing_core.spend_budget
                WHERE spend_budget_id = %s
                """,
                (spend_budget_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_spend_budget(cursor, UUID(str(row[0])))
            )

    def insert_spend_budget(self, budget: StoredSpendBudget) -> StoredSpendBudget:
        """Persist one published spend budget or return its identity replay."""
        if CURRENCY_CODE_PATTERN.fullmatch(budget.currency_code) is None:
            raise ValueError("currency_code must be a three-letter ISO code")
        if SOURCE_PAYLOAD_HASH_PATTERN.fullmatch(budget.source_payload_hash) is None:
            raise ValueError("source_payload_hash must be a sha256 digest")
        if budget.spend_budget_status != "published":
            raise ValueError("spend_budget_status must be published")
        budget_amount = parse_exact_decimal(format_exact_decimal(budget.budget_amount))
        if budget_amount <= 0:
            raise ValueError("budget amount must be a positive exact decimal")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.spend_budget
                    (spend_budget_id, tenant_account_id, billing_account_id,
                     spend_budget_contract_version, currency_code, budget_amount,
                     window_started_at, window_ended_at, source_payload_hash,
                     published_at, spend_budget_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING spend_budget_id
                """,
                (
                    budget.spend_budget_id,
                    budget.tenant_account_id,
                    budget.billing_account_id,
                    budget.spend_budget_contract_version,
                    budget.currency_code,
                    budget_amount,
                    budget.window_started_at,
                    budget.window_ended_at,
                    budget.source_payload_hash,
                    budget.published_at,
                    budget.spend_budget_status,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT spend_budget_id
                    FROM billing_core.spend_budget
                    WHERE tenant_account_id = %s
                      AND billing_account_id = %s
                      AND window_started_at = %s
                      AND window_ended_at = %s
                      AND currency_code = %s
                      AND source_payload_hash = %s
                      AND spend_budget_contract_version = %s
                    """,
                    (
                        budget.tenant_account_id,
                        budget.billing_account_id,
                        budget.window_started_at,
                        budget.window_ended_at,
                        budget.currency_code,
                        budget.source_payload_hash,
                        budget.spend_budget_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "spend budget identity conflicts with an existing row"
                    )
            return self._fetch_spend_budget(cursor, UUID(str(row[0])))

    def list_spend_budgets(
        self, tenant_account_id: UUID
    ) -> tuple[StoredSpendBudget, ...]:
        """Return spend budgets limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT spend_budget_id
                FROM billing_core.spend_budget
                WHERE tenant_account_id = %s
                ORDER BY published_at, spend_budget_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_spend_budget(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def find_journal_proposal(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped draft-only proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND invoice_draft_id = %s
                  AND source_payload_hash = %s
                  AND proposal_contract_version = %s
                  AND payment_receipt_id IS NULL
                  AND credit_adjustment_id IS NULL
                  AND collection_write_off_id IS NULL
                  AND unapplied_cash_refund_id IS NULL
                  AND unapplied_cash_id IS NULL
                  AND unapplied_cash_application_id IS NULL
                  AND issued_invoice_void_id IS NULL
                  AND issued_credit_note_void_id IS NULL
                """,
                (
                    tenant_account_id,
                    invoice_draft_id,
                    source_payload_hash,
                    proposal_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_receipt(
        self,
        tenant_account_id: UUID,
        payment_receipt_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped cash proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND payment_receipt_id = %s
                  AND source_payload_hash = %s
                  AND proposal_contract_version = %s
                """,
                (
                    tenant_account_id,
                    payment_receipt_id,
                    source_payload_hash,
                    proposal_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_credit(
        self,
        tenant_account_id: UUID,
        credit_adjustment_id: UUID,
        source_payload_hash: str,
        proposal_contract_version: int,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped credit proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND credit_adjustment_id = %s
                  AND source_payload_hash = %s
                  AND proposal_contract_version = %s
                """,
                (
                    tenant_account_id,
                    credit_adjustment_id,
                    source_payload_hash,
                    proposal_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_credit_adjustment(
        self,
        tenant_account_id: UUID,
        credit_adjustment_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped credit proposal by credit identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND credit_adjustment_id = %s
                """,
                (tenant_account_id, credit_adjustment_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_write_off(
        self,
        tenant_account_id: UUID,
        collection_write_off_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped write-off proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND collection_write_off_id = %s
                """,
                (tenant_account_id, collection_write_off_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_refund(
        self,
        tenant_account_id: UUID,
        unapplied_cash_refund_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped leftover-refund proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND unapplied_cash_refund_id = %s
                """,
                (tenant_account_id, unapplied_cash_refund_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_unapplied_cash(
        self,
        tenant_account_id: UUID,
        unapplied_cash_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped leftover proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND unapplied_cash_id = %s
                """,
                (tenant_account_id, unapplied_cash_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_unapplied_cash_application(
        self,
        tenant_account_id: UUID,
        unapplied_cash_application_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped leftover-apply proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND unapplied_cash_application_id = %s
                """,
                (tenant_account_id, unapplied_cash_application_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_issued_invoice_void(
        self,
        tenant_account_id: UUID,
        issued_invoice_void_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped unused invoice-void proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND issued_invoice_void_id = %s
                """,
                (tenant_account_id, issued_invoice_void_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_issued_credit_note_void(
        self,
        tenant_account_id: UUID,
        issued_credit_note_void_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return one tenant-scoped unused credit-note-void proposal identity."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND issued_credit_note_void_id = %s
                """,
                (tenant_account_id, issued_credit_note_void_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def find_journal_proposal_for_invoice_draft(
        self,
        tenant_account_id: UUID,
        invoice_draft_id: UUID,
    ) -> StoredJournalProposal | None:
        """Return the draft-only invoice journal for one tenant-scoped draft.

        Specialized cash, credit, write-off, leftover, apply, refund,
        invoice-void, and credit-note-void proposals share
        ``invoice_draft_id`` but are not this identity. Binding uses Billing
        ``proposal_id`` only.
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                  AND invoice_draft_id = %s
                  AND payment_receipt_id IS NULL
                  AND credit_adjustment_id IS NULL
                  AND collection_write_off_id IS NULL
                  AND unapplied_cash_refund_id IS NULL
                  AND unapplied_cash_id IS NULL
                  AND unapplied_cash_application_id IS NULL
                  AND issued_invoice_void_id IS NULL
                  AND issued_credit_note_void_id IS NULL
                """,
                (tenant_account_id, invoice_draft_id),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def insert_journal_proposal(
        self,
        journal_proposal: StoredJournalProposal,
        proposal_lines: tuple[StoredJournalProposalLine, ...],
    ) -> StoredJournalProposal:
        """Persist one balanced invoice-draft, cash, credit, write-off, leftover, apply, refund, unused invoice-void, or unused credit-note-void proposal or its replay."""
        if journal_proposal.proposal_status not in {
            "draft",
            "validated",
            "exported",
            "rejected",
        }:
            raise ValueError("journal proposals cannot be posted")
        if not proposal_lines or len(
            {line.line_number for line in proposal_lines}
        ) != len(proposal_lines):
            raise ValueError("journal proposal line numbers must be unique")
        parsed_lines = tuple(
            (
                line,
                parse_exact_decimal(format_exact_decimal(line.debit_amount)),
                parse_exact_decimal(format_exact_decimal(line.credit_amount)),
            )
            for line in proposal_lines
        )
        for line, debit_amount, credit_amount in parsed_lines:
            if line.journal_proposal_id != journal_proposal.journal_proposal_id:
                raise ValueError(
                    "journal proposal line has the wrong proposal identity"
                )
            if line.tenant_account_id != journal_proposal.tenant_account_id:
                raise ValueError("journal proposal line has the wrong tenant identity")
            if (debit_amount > 0) == (credit_amount > 0):
                raise ValueError("journal proposal lines must be debit XOR credit")
            require_postable_journal_line_amounts(debit_amount, credit_amount)
        if sum((item[1] for item in parsed_lines), parse_exact_decimal("0")) != sum(
            (item[2] for item in parsed_lines), parse_exact_decimal("0")
        ):
            raise ValueError("journal proposal lines must balance")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.journal_proposal
                    (journal_proposal_id, tenant_account_id, invoice_draft_id,
                     proposal_contract_version, idempotency_key, legal_entity_reference,
                     intended_book_role_code, transaction_currency, transaction_date,
                     accounting_date, source_payload_hash, proposed_at, proposal_status,
                     source_event_reference, payment_receipt_id, credit_adjustment_id,
                     collection_write_off_id, unapplied_cash_refund_id, unapplied_cash_id,
                     unapplied_cash_application_id, issued_invoice_void_id,
                     issued_credit_note_void_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING journal_proposal_id
                """,
                (
                    journal_proposal.journal_proposal_id,
                    journal_proposal.tenant_account_id,
                    journal_proposal.invoice_draft_id,
                    journal_proposal.proposal_contract_version,
                    journal_proposal.idempotency_key,
                    journal_proposal.legal_entity_reference,
                    journal_proposal.intended_book_role_code,
                    journal_proposal.transaction_currency,
                    journal_proposal.transaction_date,
                    journal_proposal.accounting_date,
                    journal_proposal.source_payload_hash,
                    journal_proposal.proposed_at,
                    journal_proposal.proposal_status,
                    journal_proposal.source_event_reference,
                    journal_proposal.payment_receipt_id,
                    journal_proposal.credit_adjustment_id,
                    journal_proposal.collection_write_off_id,
                    journal_proposal.unapplied_cash_refund_id,
                    journal_proposal.unapplied_cash_id,
                    journal_proposal.unapplied_cash_application_id,
                    journal_proposal.issued_invoice_void_id,
                    journal_proposal.issued_credit_note_void_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                if journal_proposal.payment_receipt_id is not None:
                    identity_value = journal_proposal.payment_receipt_id
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND payment_receipt_id = %s
                          AND source_payload_hash = %s
                          AND proposal_contract_version = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            identity_value,
                            journal_proposal.source_payload_hash,
                            journal_proposal.proposal_contract_version,
                        ),
                    )
                elif journal_proposal.credit_adjustment_id is not None:
                    identity_value = journal_proposal.credit_adjustment_id
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND credit_adjustment_id = %s
                          AND source_payload_hash = %s
                          AND proposal_contract_version = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            identity_value,
                            journal_proposal.source_payload_hash,
                            journal_proposal.proposal_contract_version,
                        ),
                    )
                elif journal_proposal.collection_write_off_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND collection_write_off_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.collection_write_off_id,
                        ),
                    )
                elif journal_proposal.unapplied_cash_refund_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND unapplied_cash_refund_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.unapplied_cash_refund_id,
                        ),
                    )
                elif journal_proposal.unapplied_cash_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND unapplied_cash_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.unapplied_cash_id,
                        ),
                    )
                elif journal_proposal.unapplied_cash_application_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND unapplied_cash_application_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.unapplied_cash_application_id,
                        ),
                    )
                elif journal_proposal.issued_invoice_void_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND issued_invoice_void_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.issued_invoice_void_id,
                        ),
                    )
                elif journal_proposal.issued_credit_note_void_id is not None:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND issued_credit_note_void_id = %s
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.issued_credit_note_void_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT journal_proposal_id
                        FROM billing_core.journal_proposal
                        WHERE tenant_account_id = %s
                          AND invoice_draft_id = %s
                          AND source_payload_hash = %s
                          AND proposal_contract_version = %s
                          AND payment_receipt_id IS NULL
                          AND credit_adjustment_id IS NULL
                          AND collection_write_off_id IS NULL
                          AND unapplied_cash_refund_id IS NULL
                          AND unapplied_cash_id IS NULL
                          AND unapplied_cash_application_id IS NULL
                          AND issued_invoice_void_id IS NULL
                          AND issued_credit_note_void_id IS NULL
                        """,
                        (
                            journal_proposal.tenant_account_id,
                            journal_proposal.invoice_draft_id,
                            journal_proposal.source_payload_hash,
                            journal_proposal.proposal_contract_version,
                        ),
                    )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "journal proposal identity conflicts with an existing row"
                    )
                return self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            for line, debit_amount, credit_amount in parsed_lines:
                cursor.execute(
                    """
                    INSERT INTO billing_core.journal_proposal_line
                        (journal_proposal_line_id, journal_proposal_id,
                         tenant_account_id, line_number, account_role_code,
                         debit_amount, credit_amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        line.journal_proposal_line_id,
                        line.journal_proposal_id,
                        line.tenant_account_id,
                        line.line_number,
                        line.account_role_code,
                        debit_amount,
                        credit_amount,
                    ),
                )
            return self._fetch_journal_proposal(
                cursor, UUID(str(journal_proposal.journal_proposal_id))
            )

    def get_journal_proposal(
        self, journal_proposal_id: UUID
    ) -> StoredJournalProposal | None:
        """Return one journal proposal by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE journal_proposal_id = %s
                """,
                (journal_proposal_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_journal_proposal(cursor, UUID(str(row[0])))
            )

    def list_journal_proposals(
        self, tenant_account_id: UUID
    ) -> tuple[StoredJournalProposal, ...]:
        """Return journal proposals limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT journal_proposal_id
                FROM billing_core.journal_proposal
                WHERE tenant_account_id = %s
                ORDER BY proposed_at, journal_proposal_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_journal_proposal(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def insert_tenant_api_credential(
        self, credential: StoredTenantApiCredential
    ) -> StoredTenantApiCredential:
        """Persist one API credential.  Secrets are never replayed or stored."""
        if credential.credential_status not in {"active", "revoked"}:
            raise ValueError("credential_status must be active or revoked")
        if not credential.credential_secret_hash.startswith("hmac-sha256:"):
            raise ValueError("credential_secret_hash must be a keyed HMAC")
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM billing_core.tenant_api_credential
                WHERE credential_secret_hash = %s
                """,
                (credential.credential_secret_hash,),
            )
            if cursor.fetchone() is not None:
                raise ValueError("credential_secret_hash already stored")
            cursor.execute(
                """
                INSERT INTO billing_core.tenant_api_credential
                    (tenant_api_credential_id, tenant_account_id,
                     tenant_api_credential_contract_version, credential_label,
                     credential_prefix, credential_secret_hash, credential_status,
                     issued_at, revoked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_api_credential_id) DO NOTHING
                RETURNING tenant_api_credential_id
                """,
                (
                    credential.tenant_api_credential_id,
                    credential.tenant_account_id,
                    credential.tenant_api_credential_contract_version,
                    credential.credential_label,
                    credential.credential_prefix,
                    credential.credential_secret_hash,
                    credential.credential_status,
                    credential.issued_at,
                    credential.revoked_at,
                ),
            )
            if cursor.fetchone() is None:
                raise ValueError("tenant_api_credential_id already stored")
        return credential

    def get_tenant_api_credential(
        self, tenant_api_credential_id: UUID
    ) -> StoredTenantApiCredential | None:
        """Return one API credential by internal identifier, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_api_credential_id, tenant_account_id,
                       tenant_api_credential_contract_version, credential_label,
                       credential_prefix, credential_secret_hash, credential_status,
                       issued_at, revoked_at
                FROM billing_core.tenant_api_credential
                WHERE tenant_api_credential_id = %s
                """,
                (tenant_api_credential_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._tenant_api_credential_from_row(row)

    def find_tenant_api_credential_by_hash(
        self, credential_secret_hash: str
    ) -> StoredTenantApiCredential | None:
        """Return the credential for one keyed hash, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_api_credential_id, tenant_account_id,
                       tenant_api_credential_contract_version, credential_label,
                       credential_prefix, credential_secret_hash, credential_status,
                       issued_at, revoked_at
                FROM billing_core.tenant_api_credential
                WHERE credential_secret_hash = %s
                """,
                (credential_secret_hash,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._tenant_api_credential_from_row(row)

    def list_tenant_api_credentials(
        self, tenant_account_id: UUID
    ) -> tuple[StoredTenantApiCredential, ...]:
        """Return API credentials limited to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_api_credential_id, tenant_account_id,
                       tenant_api_credential_contract_version, credential_label,
                       credential_prefix, credential_secret_hash, credential_status,
                       issued_at, revoked_at
                FROM billing_core.tenant_api_credential
                WHERE tenant_account_id = %s
                ORDER BY issued_at, tenant_api_credential_id
                """,
                (tenant_account_id,),
            )
            rows = cursor.fetchall()
        return tuple(self._tenant_api_credential_from_row(row) for row in rows)

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
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE billing_core.tenant_api_credential
                SET credential_status = 'revoked', revoked_at = %s
                WHERE tenant_api_credential_id = %s
                  AND credential_status = 'active'
                RETURNING tenant_api_credential_id
                """,
                (revoked_at, tenant_api_credential_id),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT 1
                    FROM billing_core.tenant_api_credential
                    WHERE tenant_api_credential_id = %s
                    """,
                    (tenant_api_credential_id,),
                )
                if cursor.fetchone() is None:
                    raise ValueError(
                        "api credential revocation requires a stored credential"
                    )
            cursor.execute(
                """
                SELECT tenant_api_credential_id, tenant_account_id,
                       tenant_api_credential_contract_version, credential_label,
                       credential_prefix, credential_secret_hash, credential_status,
                       issued_at, revoked_at
                FROM billing_core.tenant_api_credential
                WHERE tenant_api_credential_id = %s
                """,
                (tenant_api_credential_id,),
            )
            stored_row = cursor.fetchone()
        assert stored_row is not None
        return self._tenant_api_credential_from_row(stored_row)

    @staticmethod
    def _tenant_api_credential_from_row(
        row: tuple[Any, ...],
    ) -> StoredTenantApiCredential:
        """Decode one normalized API credential row."""
        return StoredTenantApiCredential(
            UUID(str(row[0])),
            UUID(str(row[1])),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            row[7],
            row[8],
        )

    def insert_webhook_subscription(
        self, subscription: StoredWebhookSubscription
    ) -> StoredWebhookSubscription:
        """Persist one subscription; a unique identity returns its replay."""
        if subscription.subscription_status not in {"active", "revoked"}:
            raise ValueError("subscription_status must be active or revoked")
        if not subscription.webhook_secret_hash.startswith("hmac-sha256:"):
            raise ValueError("webhook_secret_hash must be a keyed HMAC")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.webhook_subscription
                    (webhook_subscription_id, tenant_account_id,
                     webhook_subscription_contract_version, callback_url,
                     event_type_set, webhook_secret_prefix, webhook_secret_hash,
                     subscription_status, issued_at, revoked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING webhook_subscription_id
                """,
                (
                    subscription.webhook_subscription_id,
                    subscription.tenant_account_id,
                    subscription.webhook_subscription_contract_version,
                    subscription.callback_url,
                    subscription.event_type_set,
                    subscription.webhook_secret_prefix,
                    subscription.webhook_secret_hash,
                    subscription.subscription_status,
                    subscription.issued_at,
                    subscription.revoked_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT webhook_subscription_id
                    FROM billing_core.webhook_subscription
                    WHERE tenant_account_id = %s
                      AND callback_url = %s
                      AND event_type_set = %s
                      AND webhook_subscription_contract_version = %s
                    """,
                    (
                        subscription.tenant_account_id,
                        subscription.callback_url,
                        subscription.event_type_set,
                        subscription.webhook_subscription_contract_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "webhook subscription identity already belongs to another row"
                    )
            return self._fetch_webhook_subscription(cursor, UUID(str(row[0])))

    def store_webhook_subscription_secret(
        self, webhook_subscription_id: UUID, webhook_secret: str
    ) -> None:
        """Keep the one-time secret in the worker process; SQL stores only its hash."""
        if not webhook_secret:
            raise ValueError("webhook secret must be a non-empty string")
        self.webhook_subscription_secrets[webhook_subscription_id] = webhook_secret

    def get_webhook_subscription_secret(
        self, webhook_subscription_id: UUID
    ) -> str | None:
        """Return the process-local secret for one subscription, if present."""
        return self.webhook_subscription_secrets.get(webhook_subscription_id)

    def get_webhook_subscription(
        self, webhook_subscription_id: UUID
    ) -> StoredWebhookSubscription | None:
        """Return one subscription by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT webhook_subscription_id
                FROM billing_core.webhook_subscription
                WHERE webhook_subscription_id = %s
                """,
                (webhook_subscription_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_webhook_subscription(cursor, UUID(str(row[0])))
            )

    def find_webhook_subscription(
        self,
        tenant_account_id: UUID,
        callback_url: str,
        event_type_set: str,
        webhook_subscription_contract_version: int,
    ) -> StoredWebhookSubscription | None:
        """Return one tenant-scoped subscription identity, if present."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT webhook_subscription_id
                FROM billing_core.webhook_subscription
                WHERE tenant_account_id = %s
                  AND callback_url = %s
                  AND event_type_set = %s
                  AND webhook_subscription_contract_version = %s
                """,
                (
                    tenant_account_id,
                    callback_url,
                    event_type_set,
                    webhook_subscription_contract_version,
                ),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_webhook_subscription(cursor, UUID(str(row[0])))
            )

    def list_webhook_subscriptions(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookSubscription, ...]:
        """Return subscription metadata limited to one tenant."""
        return self._list_webhook_subscriptions(tenant_account_id)

    def list_active_webhook_subscriptions(
        self, tenant_account_id: UUID, event_type_code: str
    ) -> tuple[StoredWebhookSubscription, ...]:
        """Return active same-tenant subscriptions containing one event code."""
        return self._list_webhook_subscriptions(
            tenant_account_id, event_type_code=event_type_code
        )

    def revoke_webhook_subscription(
        self, webhook_subscription_id: UUID, revoked_at: datetime
    ) -> StoredWebhookSubscription:
        """Revoke one subscription idempotently."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE billing_core.webhook_subscription
                SET subscription_status = 'revoked', revoked_at = %s
                WHERE webhook_subscription_id = %s
                  AND subscription_status = 'active'
                RETURNING webhook_subscription_id
                """,
                (revoked_at, webhook_subscription_id),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT webhook_subscription_id
                    FROM billing_core.webhook_subscription
                    WHERE webhook_subscription_id = %s
                    """,
                    (webhook_subscription_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "webhook subscription revocation requires a stored subscription"
                    )
            return self._fetch_webhook_subscription(cursor, UUID(str(row[0])))

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
            return (
                None
                if row is None
                else self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))
            )

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
            return (
                None
                if row is None
                else self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))
            )

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
                    raise ValueError(
                        "outbox event identity already belongs to another row"
                    )
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

    def mark_webhook_outbox_event_delivered(
        self, outbox_event_id: UUID
    ) -> StoredWebhookOutboxEvent:
        """Mark one outbox event delivered idempotently."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE billing_core.webhook_outbox_event
                SET delivery_status = 'delivered'
                WHERE outbox_event_id = %s
                  AND delivery_status = 'pending'
                RETURNING outbox_event_id
                """,
                (outbox_event_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT outbox_event_id
                    FROM billing_core.webhook_outbox_event
                    WHERE outbox_event_id = %s
                    """,
                    (outbox_event_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("outbox delivery requires a stored event")
            return self._fetch_webhook_outbox_event(cursor, UUID(str(row[0])))

    def insert_webhook_delivery_attempt(
        self, attempt: StoredWebhookDeliveryAttempt
    ) -> StoredWebhookDeliveryAttempt:
        """Append one delivery attempt; an exact replay returns the stored row."""
        if attempt.attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_core.webhook_delivery_attempt
                    (delivery_attempt_id, outbox_event_id, webhook_subscription_id,
                     tenant_account_id, attempt_number, http_status, delivered_at,
                     failure_reason_code, attempted_at)
                SELECT %s, %s, %s, o.tenant_account_id, %s, %s, %s, %s, %s
                FROM billing_core.webhook_outbox_event AS o
                WHERE o.outbox_event_id = %s
                ON CONFLICT DO NOTHING
                RETURNING delivery_attempt_id
                """,
                (
                    attempt.delivery_attempt_id,
                    attempt.outbox_event_id,
                    attempt.webhook_subscription_id,
                    attempt.attempt_number,
                    attempt.http_status,
                    attempt.delivered_at,
                    attempt.failure_reason_code,
                    attempt.attempted_at,
                    attempt.outbox_event_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT delivery_attempt_id
                    FROM billing_core.webhook_delivery_attempt
                    WHERE outbox_event_id = %s
                      AND webhook_subscription_id = %s
                      AND attempt_number = %s
                    """,
                    (
                        attempt.outbox_event_id,
                        attempt.webhook_subscription_id,
                        attempt.attempt_number,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        "delivery attempt identity already belongs to another row"
                    )
            return self._fetch_webhook_delivery_attempt(cursor, UUID(str(row[0])))

    def list_webhook_delivery_attempts(
        self, outbox_event_id: UUID, webhook_subscription_id: UUID | None = None
    ) -> tuple[StoredWebhookDeliveryAttempt, ...]:
        """Return attempts for one outbox event, optionally one subscription."""
        with self._cursor() as cursor:
            if webhook_subscription_id is None:
                cursor.execute(
                    """
                    SELECT delivery_attempt_id
                    FROM billing_core.webhook_delivery_attempt
                    WHERE outbox_event_id = %s
                    ORDER BY attempt_number, delivery_attempt_id
                    """,
                    (outbox_event_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT delivery_attempt_id
                    FROM billing_core.webhook_delivery_attempt
                    WHERE outbox_event_id = %s
                      AND webhook_subscription_id = %s
                    ORDER BY attempt_number, delivery_attempt_id
                    """,
                    (outbox_event_id, webhook_subscription_id),
                )
            return tuple(
                self._fetch_webhook_delivery_attempt(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def get_webhook_delivery_attempt(
        self, delivery_attempt_id: UUID
    ) -> StoredWebhookDeliveryAttempt | None:
        """Return one delivery attempt by opaque identifier."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT delivery_attempt_id
                FROM billing_core.webhook_delivery_attempt
                WHERE delivery_attempt_id = %s
                """,
                (delivery_attempt_id,),
            )
            row = cursor.fetchone()
            return (
                None
                if row is None
                else self._fetch_webhook_delivery_attempt(cursor, UUID(str(row[0])))
            )

    def list_webhook_delivery_attempts_for_tenant(
        self, tenant_account_id: UUID
    ) -> tuple[StoredWebhookDeliveryAttempt, ...]:
        """Return attempts whose outbox belongs to one tenant."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT a.delivery_attempt_id
                FROM billing_core.webhook_delivery_attempt AS a
                JOIN billing_core.webhook_outbox_event AS o
                  ON o.tenant_account_id = a.tenant_account_id
                 AND o.outbox_event_id = a.outbox_event_id
                WHERE a.tenant_account_id = %s
                ORDER BY a.attempted_at, a.delivery_attempt_id
                """,
                (tenant_account_id,),
            )
            return tuple(
                self._fetch_webhook_delivery_attempt(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    def stored_usage_set(
        self, tenant_account_id: UUID
    ) -> frozenset[tuple[object, ...]]:
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

    def _require_principal(
        self, tenant: TenantAccount, reference: str
    ) -> BillingPrincipal:
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

    def _require_credential(
        self, tenant: TenantAccount, reference: str
    ) -> CredentialRecord:
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

    @staticmethod
    def _tenant_account_id_with_cursor(cursor: Any, tenant_reference: str) -> UUID:
        """Resolve a tenant inside an existing transaction without opening a cursor."""
        cursor.execute(
            """
            SELECT tenant_account_id
            FROM billing_core.tenant_account
            WHERE tenant_reference = %s
            """,
            (tenant_reference,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(tenant_reference)
        return UUID(str(row[0]))

    @staticmethod
    def _fetch_billing_period(
        cursor: Any,
        period_id: UUID,
        *,
        lock: bool = False,
        tenant_account_id: UUID | None = None,
    ) -> BillingPeriod | None:
        """Hydrate a period and its append-only transitions on one cursor."""
        query = """
            SELECT period_id, tenant_account_id, tenant_reference, period_start,
                   period_end, opened_at, opened_by, period_contract_version
            FROM billing_core.billing_period
            JOIN billing_core.tenant_account USING (tenant_account_id)
            WHERE period_id = %s
        """
        parameters: tuple[Any, ...] = (period_id,)
        if tenant_account_id is not None:
            query += " AND billing_period.tenant_account_id = %s"
            parameters += (tenant_account_id,)
        if lock:
            query += " FOR UPDATE"
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            SELECT transition_number, transition_id, from_status, to_status, actor_reference,
                   authorization_reference, transition_reason, transitioned_at
            FROM billing_core.billing_period_transition
            WHERE tenant_account_id = %s AND period_id = %s
            ORDER BY transition_number
            """,
            (row[1], period_id),
        )
        transitions = tuple(
            BillingPeriodTransition(
                transition_id=UUID(str(transition[1])),
                from_status=transition[2],
                to_status=transition[3],
                actor_reference=transition[4],
                authorization_reference=transition[5],
                reason=transition[6],
                transitioned_at=transition[7],
            )
            for transition in cursor.fetchall()
        )
        return BillingPeriod(
            period_id=UUID(str(row[0])),
            tenant_reference=row[2],
            period_start=row[3],
            period_end=row[4],
            opened_at=row[5],
            opened_by=row[6],
            status=transitions[-1].to_status
            if transitions
            else BillingPeriodStatus.OPEN,
            transitions=transitions,
            period_contract_version=row[7],
        )

    @staticmethod
    def _fetch_late_adjustment(
        cursor: Any, late_adjustment_id: UUID, tenant_account_id: UUID
    ) -> LateAdjustment | None:
        """Hydrate one later-period adjustment within its tenant boundary."""
        cursor.execute(
            """
            SELECT late_adjustment_id, source_period_id, target_period_id,
                   adjustment_kind, adjustment_amount, currency_code,
                   source_reference, source_payload_hash, recorded_at,
                   late_adjustment_contract_version
            FROM billing_core.late_adjustment
            WHERE late_adjustment_id = %s AND tenant_account_id = %s
            """,
            (late_adjustment_id, tenant_account_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return LateAdjustment(
            late_adjustment_id=UUID(str(row[0])),
            source_period_id=UUID(str(row[1])),
            target_period_id=UUID(str(row[2])),
            adjustment_kind=row[3],
            adjustment_amount=row[4],
            currency_code=row[5],
            source_reference=row[6],
            source_payload_hash=row[7],
            recorded_at=row[8],
            late_adjustment_contract_version=row[9],
        )

    @staticmethod
    def _fetch_late_adjustment_application(
        cursor: Any,
        late_adjustment_application_id: UUID,
        tenant_account_id: UUID | None = None,
    ) -> StoredLateAdjustmentApplication | None:
        """Hydrate one application, optionally enforcing its tenant boundary."""
        if tenant_account_id is None:
            cursor.execute(
                """
                SELECT late_adjustment_application_id, tenant_account_id,
                       late_adjustment_id, target_period_id, adjustment_amount,
                       currency_code, applied_by, authorization_reference, applied_at,
                       late_adjustment_application_contract_version,
                       late_adjustment_application_status
                FROM billing_core.late_adjustment_application
                WHERE late_adjustment_application_id = %s
                """,
                (late_adjustment_application_id,),
            )
        else:
            cursor.execute(
                """
                SELECT late_adjustment_application_id, tenant_account_id,
                       late_adjustment_id, target_period_id, adjustment_amount,
                       currency_code, applied_by, authorization_reference, applied_at,
                       late_adjustment_application_contract_version,
                       late_adjustment_application_status
                FROM billing_core.late_adjustment_application
                WHERE late_adjustment_application_id = %s
                  AND tenant_account_id = %s
                """,
                (late_adjustment_application_id, tenant_account_id),
            )
        row = cursor.fetchone()
        if row is None:
            return None
        return StoredLateAdjustmentApplication(
            late_adjustment_application_id=UUID(str(row[0])),
            tenant_account_id=UUID(str(row[1])),
            late_adjustment_id=UUID(str(row[2])),
            target_period_id=UUID(str(row[3])),
            adjustment_amount=row[4],
            currency_code=row[5],
            applied_by=row[6],
            authorization_reference=row[7],
            applied_at=row[8],
            late_adjustment_application_contract_version=row[9],
            late_adjustment_application_status=row[10],
        )

    @staticmethod
    def _fetch_late_adjustment_rating(
        cursor: Any,
        late_adjustment_rating_id: UUID,
        tenant_account_id: UUID | None = None,
    ) -> StoredLateAdjustmentRating | None:
        """Hydrate one rating fact, optionally enforcing its tenant boundary."""
        query = """
            SELECT late_adjustment_rating_id, tenant_account_id,
                   late_adjustment_application_id, late_adjustment_id,
                   target_period_id, adjustment_amount, currency_code,
                   rated_by, authorization_reference, rated_at,
                   late_adjustment_rating_contract_version,
                   late_adjustment_rating_status
            FROM billing_core.late_adjustment_rating
            WHERE late_adjustment_rating_id = %s
        """
        parameters: tuple[object, ...] = (late_adjustment_rating_id,)
        if tenant_account_id is not None:
            query += " AND tenant_account_id = %s"
            parameters += (tenant_account_id,)
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        return StoredLateAdjustmentRating(
            late_adjustment_rating_id=UUID(str(row[0])),
            tenant_account_id=UUID(str(row[1])),
            late_adjustment_application_id=UUID(str(row[2])),
            late_adjustment_id=UUID(str(row[3])),
            target_period_id=UUID(str(row[4])),
            adjustment_amount=row[5],
            currency_code=row[6],
            rated_by=row[7],
            authorization_reference=row[8],
            rated_at=row[9],
            late_adjustment_rating_contract_version=row[10],
            late_adjustment_rating_status=row[11],
        )

    @staticmethod
    def _fetch_fx_rate(cursor: Any, fx_rate_id: UUID) -> FxRate | None:
        """Hydrate one exact FX rate snapshot."""
        cursor.execute(
            """
            SELECT fx_rate_id, rate_source, rate_type, base_currency, quote_currency,
                   fx_rate_value, rate_precision, effective_at, recorded_at,
                   fx_rate_contract_version
            FROM billing_core.fx_rate
            WHERE fx_rate_id = %s
            """,
            (fx_rate_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return FxRate(
            fx_rate_id=UUID(str(row[0])),
            rate_source=row[1],
            rate_type=row[2],
            base_currency=row[3],
            quote_currency=row[4],
            rate=row[5],
            rate_precision=row[6],
            effective_at=row[7],
            recorded_at=row[8],
            fx_rate_contract_version=row[9],
        )

    @staticmethod
    def _fetch_fx_conversion(
        cursor: Any, fx_conversion_id: UUID
    ) -> FxConversion | None:
        """Hydrate one frozen conversion result."""
        cursor.execute(
            """
            SELECT fx_conversion_id, fx_rate_id, source_amount, source_currency,
                   quote_amount, quote_currency, quote_minor_units, fx_rate_value,
                   rate_precision, rounding_mode, converted_at,
                   fx_conversion_contract_version
            FROM billing_core.fx_conversion
            WHERE fx_conversion_id = %s
            """,
            (fx_conversion_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row[9] != "ROUND_HALF_UP":  # pragma: no cover - protected by the table check
            raise ValueError("unsupported FX rounding mode")
        return FxConversion(
            fx_conversion_id=UUID(str(row[0])),
            fx_rate_id=UUID(str(row[1])),
            source_amount=row[2],
            source_currency=row[3],
            quote_amount=row[4],
            quote_currency=row[5],
            quote_minor_units=row[6],
            rate=row[7],
            rate_precision=row[8],
            converted_at=row[10],
            fx_conversion_contract_version=row[11],
        )

    @staticmethod
    def _fetch_reconciliation_line(
        cursor: Any,
        reconciliation_line_id: UUID,
        *,
        tenant_account_id: UUID | None = None,
    ) -> ReconciliationLine | None:
        """Hydrate one reconciliation line and its normalized exception children."""
        query = """
            SELECT reconciliation_line_id, period_id, provider_account_reference,
                   currency_code, internal_currency_code, provider_currency_code,
                   cash_currency_code, internal_expected_amount, provider_actual_amount,
                   cash_actual_amount, provider_fee_amount, withheld_tax_amount,
                   reserve_amount, expected_cash_amount, reconciliation_line_status,
                   assessed_at, reconciliation_line_contract_version
            FROM billing_core.reconciliation_line
            WHERE reconciliation_line_id = %s
        """
        parameters: tuple[Any, ...] = (reconciliation_line_id,)
        if tenant_account_id is not None:
            query += " AND tenant_account_id = %s"
            parameters += (tenant_account_id,)
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            SELECT exception_code, next_action
            FROM billing_core.reconciliation_exception
            WHERE reconciliation_line_id = %s
            ORDER BY exception_number
            """,
            (reconciliation_line_id,),
        )
        exceptions = tuple(
            ReconciliationException(exception_code=item[0], next_action=item[1])
            for item in cursor.fetchall()
        )
        return ReconciliationLine(
            reconciliation_line_id=UUID(str(row[0])),
            period_id=UUID(str(row[1])),
            provider_account_reference=row[2],
            currency_code=row[3],
            internal_currency_code=row[4],
            provider_currency_code=row[5],
            cash_currency_code=row[6],
            internal_expected_amount=row[7],
            provider_actual_amount=row[8],
            cash_actual_amount=row[9],
            provider_fee_amount=row[10],
            withheld_tax_amount=row[11],
            reserve_amount=row[12],
            expected_cash_amount=row[13],
            status=row[14],
            exceptions=exceptions,
            assessed_at=row[15],
            reconciliation_line_contract_version=row[16],
        )

    @staticmethod
    def _fetch_reconciliation_resolution(
        cursor: Any,
        resolution_id: UUID,
        *,
        tenant_account_id: UUID | None = None,
    ) -> ReconciliationResolution | None:
        """Hydrate one immutable maker-checker resolution."""
        query = """
            SELECT resolution_id, reconciliation_line_id, exception_code,
                   resolution_status, owner_reference, resolution_reason,
                   evidence_reference, maker_reference, checker_reference,
                   resolved_at, reconciliation_resolution_contract_version
            FROM billing_core.reconciliation_resolution
            WHERE resolution_id = %s
        """
        parameters: tuple[Any, ...] = (resolution_id,)
        if tenant_account_id is not None:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM billing_core.reconciliation_line
                    WHERE reconciliation_line_id =
                        reconciliation_resolution.reconciliation_line_id
                      AND tenant_account_id = %s
                )
            """
            parameters += (tenant_account_id,)
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        return ReconciliationResolution(
            resolution_id=UUID(str(row[0])),
            reconciliation_line_id=UUID(str(row[1])),
            exception_code=row[2],
            resolution_status=row[3],
            owner_reference=row[4],
            resolution_reason=row[5],
            evidence_reference=row[6],
            maker_reference=row[7],
            checker_reference=row[8],
            resolved_at=row[9],
            reconciliation_resolution_contract_version=row[10],
        )

    @staticmethod
    def _fetch_reconciliation_evidence(
        cursor: Any,
        evidence_id: UUID,
        *,
        tenant_account_id: UUID | None = None,
    ) -> ReconciliationEvidence | None:
        """Hydrate one immutable hash-backed evidence record."""
        query = """
            SELECT evidence_id, reconciliation_line_id, exception_code, evidence_kind,
                   evidence_reference, evidence_sha256, captured_by, captured_at,
                   reconciliation_evidence_contract_version
            FROM billing_core.reconciliation_evidence
            WHERE evidence_id = %s
        """
        parameters: tuple[Any, ...] = (evidence_id,)
        if tenant_account_id is not None:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM billing_core.reconciliation_line
                    WHERE reconciliation_line_id = reconciliation_evidence.reconciliation_line_id
                      AND tenant_account_id = %s
                )
            """
            parameters += (tenant_account_id,)
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        return ReconciliationEvidence(
            evidence_id=UUID(str(row[0])),
            reconciliation_line_id=UUID(str(row[1])),
            exception_code=row[2],
            evidence_kind=row[3],
            evidence_reference=row[4],
            evidence_sha256=row[5],
            captured_by=row[6],
            captured_at=row[7],
            reconciliation_evidence_contract_version=row[8],
        )

    @staticmethod
    def _fetch_reconciliation_run(
        cursor: Any,
        run_id: UUID,
        *,
        tenant_account_id: UUID | None = None,
    ) -> ReconciliationRun | None:
        """Hydrate one completed run with ordered line membership."""
        query = """
            SELECT run_id, period_id, started_at, completed_at,
                   blocking_exception_count, reconciliation_run_contract_version
            FROM billing_core.reconciliation_run
            WHERE run_id = %s
        """
        parameters: tuple[Any, ...] = (run_id,)
        if tenant_account_id is not None:
            query += " AND tenant_account_id = %s"
            parameters += (tenant_account_id,)
        cursor.execute(query, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        line_query = """
            SELECT reconciliation_line_id
            FROM billing_core.reconciliation_run_line
            WHERE run_id = %s
        """
        line_parameters: tuple[Any, ...] = (run_id,)
        if tenant_account_id is not None:
            line_query += " AND tenant_account_id = %s"
            line_parameters += (tenant_account_id,)
        line_query += " ORDER BY line_number"
        cursor.execute(line_query, line_parameters)
        line_ids = tuple(UUID(str(item[0])) for item in cursor.fetchall())
        return ReconciliationRun(
            run_id=UUID(str(row[0])),
            period_id=UUID(str(row[1])),
            started_at=row[2],
            completed_at=row[3],
            reconciliation_line_ids=line_ids,
            blocking_exception_count=row[4],
            reconciliation_run_contract_version=row[5],
        )

    def _find_event(
        self, query: str, parameters: tuple[Any, ...]
    ) -> StoredUsageEvent | None:
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
                (
                    tenant_account_id,
                    event.event_payload_hash,
                    event.event_contract_version,
                ),
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
        measurements = tuple(
            self._measurement_from_row(measurement) for measurement in cursor.fetchall()
        )
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

    def _fetch_invoice_draft(
        self, cursor: Any, invoice_draft_id: UUID
    ) -> StoredInvoiceDraft:
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
    def _collection_case_from_row(row: tuple[Any, ...]) -> StoredCollectionCase:
        """Decode one normalized collection-case row."""
        return StoredCollectionCase(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            parse_exact_decimal(format_exact_decimal(row[5])),
            row[6],
        )

    def _fetch_collection_case(
        self, cursor: Any, collection_case_id: UUID
    ) -> StoredCollectionCase:
        """Hydrate one collection case."""
        cursor.execute(
            """
            SELECT collection_case_id, tenant_account_id, invoice_draft_id,
                   currency_code, collection_case_status, outstanding_amount, opened_at
            FROM billing_core.collection_case
            WHERE collection_case_id = %s
            """,
            (collection_case_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(collection_case_id)
        return self._collection_case_from_row(row)

    @staticmethod
    def _collection_dunning_event_from_row(
        row: tuple[Any, ...],
    ) -> StoredCollectionDunningEvent:
        """Decode one normalized dunning-event row."""
        return StoredCollectionDunningEvent(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
        )

    def _fetch_collection_dunning_event(
        self, cursor: Any, collection_dunning_event_id: UUID
    ) -> StoredCollectionDunningEvent:
        """Hydrate one dunning event."""
        cursor.execute(
            """
            SELECT collection_dunning_event_id, collection_case_id,
                   tenant_account_id, dunning_event_number, dunning_notice_code,
                   occurred_at
            FROM billing_core.collection_dunning_event
            WHERE collection_dunning_event_id = %s
            """,
            (collection_dunning_event_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(collection_dunning_event_id)
        return self._collection_dunning_event_from_row(row)

    def _list_collection_dunning_events(
        self,
        *,
        collection_case_id: UUID | None = None,
        tenant_account_id: UUID | None = None,
    ) -> tuple[StoredCollectionDunningEvent, ...]:
        """List dunning events by case or tenant with an explicit predicate."""
        with self._cursor() as cursor:
            if collection_case_id is not None:
                cursor.execute(
                    """
                    SELECT collection_dunning_event_id
                    FROM billing_core.collection_dunning_event
                    WHERE collection_case_id = %s
                    ORDER BY dunning_event_number, collection_dunning_event_id
                    """,
                    (collection_case_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT collection_dunning_event_id
                    FROM billing_core.collection_dunning_event
                    WHERE tenant_account_id = %s
                    ORDER BY occurred_at, collection_dunning_event_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(
                self._fetch_collection_dunning_event(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
            )

    @staticmethod
    def _payment_intent_from_row(row: tuple[Any, ...]) -> StoredPaymentIntent:
        """Decode one normalized payment-intent row."""
        return StoredPaymentIntent(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            parse_exact_decimal(format_exact_decimal(row[6])),
            row[7],
            row[8],
        )

    def _fetch_payment_intent(
        self, cursor: Any, payment_intent_id: UUID
    ) -> StoredPaymentIntent:
        """Hydrate one payment intent."""
        cursor.execute(
            """
            SELECT payment_intent_id, tenant_account_id, collection_case_id,
                   payment_intent_contract_version, currency_code,
                   payment_intent_status, payment_amount, source_payload_hash,
                   projected_at
            FROM billing_core.payment_intent
            WHERE payment_intent_id = %s
            """,
            (payment_intent_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(payment_intent_id)
        return self._payment_intent_from_row(row)

    @staticmethod
    def _payment_receipt_from_row(row: tuple[Any, ...]) -> StoredPaymentReceipt:
        """Decode one normalized payment-receipt row."""
        return StoredPaymentReceipt(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            row[4],
            row[5],
            row[6],
            parse_exact_decimal(format_exact_decimal(row[7])),
            row[8],
            row[9],
        )

    def _fetch_payment_receipt(
        self, cursor: Any, payment_receipt_id: UUID
    ) -> StoredPaymentReceipt:
        """Hydrate one payment receipt."""
        cursor.execute(
            """
            SELECT payment_receipt_id, tenant_account_id, payment_intent_id,
                   collection_case_id, settlement_contract_version, currency_code,
                   payment_receipt_status, received_amount, source_payload_hash,
                   received_at
            FROM billing_core.payment_receipt
            WHERE payment_receipt_id = %s
            """,
            (payment_receipt_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(payment_receipt_id)
        return self._payment_receipt_from_row(row)

    @staticmethod
    def _unapplied_cash_from_row(row: tuple[Any, ...]) -> StoredUnappliedCash:
        """Decode one normalized parked leftover row."""
        return StoredUnappliedCash(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            parse_exact_decimal(format_exact_decimal(row[8])),
            parse_exact_decimal(format_exact_decimal(row[9])),
            parse_exact_decimal(format_exact_decimal(row[10])),
            row[11],
            row[12],
        )

    def _fetch_unapplied_cash(
        self, cursor: Any, unapplied_cash_id: UUID
    ) -> StoredUnappliedCash:
        """Hydrate one parked leftover."""
        cursor.execute(
            """
            SELECT unapplied_cash_id, tenant_account_id, payment_receipt_id,
                   payment_intent_id, collection_case_id,
                   unapplied_cash_contract_version, source_payload_hash,
                   currency_code, unapplied_amount, received_amount,
                   applied_amount, unapplied_cash_status, parked_at
            FROM billing_core.unapplied_cash
            WHERE unapplied_cash_id = %s
            """,
            (unapplied_cash_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(unapplied_cash_id)
        return self._unapplied_cash_from_row(row)

    @staticmethod
    def _unapplied_cash_application_from_row(
        row: tuple[Any, ...],
    ) -> StoredUnappliedCashApplication:
        """Decode one normalized leftover-apply row."""
        return StoredUnappliedCashApplication(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            UUID(str(row[5])),
            row[6],
            row[7],
            row[8],
            parse_exact_decimal(format_exact_decimal(row[9])),
            row[10],
            row[11],
        )

    def _fetch_unapplied_cash_application(
        self, cursor: Any, unapplied_cash_application_id: UUID
    ) -> StoredUnappliedCashApplication:
        """Hydrate one leftover apply."""
        cursor.execute(
            """
            SELECT unapplied_cash_application_id, tenant_account_id,
                   unapplied_cash_id, collection_case_id, payment_receipt_id,
                   invoice_draft_id, unapplied_cash_application_contract_version,
                   source_payload_hash, currency_code, applied_amount,
                   unapplied_cash_application_status, applied_at
            FROM billing_core.unapplied_cash_application
            WHERE unapplied_cash_application_id = %s
            """,
            (unapplied_cash_application_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(unapplied_cash_application_id)
        return self._unapplied_cash_application_from_row(row)

    @staticmethod
    def _unapplied_cash_refund_from_row(
        row: tuple[Any, ...],
    ) -> StoredUnappliedCashRefund:
        """Decode one normalized leftover-refund row."""
        return StoredUnappliedCashRefund(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            UUID(str(row[5])),
            row[6],
            row[7],
            row[8],
            parse_exact_decimal(format_exact_decimal(row[9])),
            parse_exact_decimal(format_exact_decimal(row[10])),
            row[11],
            row[12],
        )

    def _fetch_unapplied_cash_refund(
        self, cursor: Any, unapplied_cash_refund_id: UUID
    ) -> StoredUnappliedCashRefund:
        """Hydrate one leftover refund."""
        cursor.execute(
            """
            SELECT unapplied_cash_refund_id, tenant_account_id, unapplied_cash_id,
                   payment_receipt_id, payment_intent_id, collection_case_id,
                   unapplied_cash_refund_contract_version, source_payload_hash,
                   currency_code, refund_amount, unapplied_amount,
                   unapplied_cash_refund_status, refunded_at
            FROM billing_core.unapplied_cash_refund
            WHERE unapplied_cash_refund_id = %s
            """,
            (unapplied_cash_refund_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(unapplied_cash_refund_id)
        return self._unapplied_cash_refund_from_row(row)

    @staticmethod
    def _credit_adjustment_from_row(row: tuple[Any, ...]) -> StoredCreditAdjustment:
        """Decode one normalized credit-adjustment row."""
        return StoredCreditAdjustment(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            parse_exact_decimal(format_exact_decimal(row[6])),
            parse_exact_decimal(format_exact_decimal(row[7])),
            parse_exact_decimal(format_exact_decimal(row[8])),
            row[9],
            row[10],
        )

    def _fetch_credit_adjustment(
        self, cursor: Any, credit_adjustment_id: UUID
    ) -> StoredCreditAdjustment:
        """Hydrate one credit adjustment."""
        cursor.execute(
            """
            SELECT credit_adjustment_id, tenant_account_id, invoice_draft_id,
                   credit_adjustment_contract_version, credit_reason_code,
                   currency_code, credit_amount, tax_exclusive_amount, tax_amount,
                   source_payload_hash, recorded_at
            FROM billing_core.credit_adjustment
            WHERE credit_adjustment_id = %s
            """,
            (credit_adjustment_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(credit_adjustment_id)
        return self._credit_adjustment_from_row(row)

    @staticmethod
    def _issued_credit_note_from_row(row: tuple[Any, ...]) -> StoredIssuedCreditNote:
        """Decode one normalized issued-credit-note row."""
        return StoredIssuedCreditNote(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            None if row[4] is None else UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            parse_exact_decimal(format_exact_decimal(row[11])),
            parse_exact_decimal(format_exact_decimal(row[12])),
            parse_exact_decimal(format_exact_decimal(row[13])),
            row[14],
            row[15],
        )

    def _fetch_issued_credit_note(
        self, cursor: Any, issued_credit_note_id: UUID
    ) -> StoredIssuedCreditNote:
        """Hydrate one issued credit-note snapshot."""
        cursor.execute(
            """
            SELECT issued_credit_note_id, tenant_account_id, credit_adjustment_id,
                   invoice_draft_id, issued_invoice_id,
                   issued_credit_note_contract_version,
                   credit_adjustment_contract_version, credit_reason_code,
                   credit_adjustment_source_payload_hash, source_payload_hash,
                   currency_code, tax_exclusive_amount, tax_amount,
                   tax_inclusive_amount, issued_credit_note_status, issued_at
            FROM billing_core.issued_credit_note
            WHERE issued_credit_note_id = %s
            """,
            (issued_credit_note_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(issued_credit_note_id)
        return self._issued_credit_note_from_row(row)

    @staticmethod
    def _issued_invoice_void_from_row(row: tuple[Any, ...]) -> StoredIssuedInvoiceVoid:
        """Decode one normalized unused issued-invoice void row."""
        return StoredIssuedInvoiceVoid(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            None if row[4] is None else UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            parse_exact_decimal(format_exact_decimal(row[8])),
            parse_exact_decimal(format_exact_decimal(row[9])),
            row[10],
            row[11],
        )

    def _fetch_issued_invoice_void(
        self, cursor: Any, issued_invoice_void_id: UUID
    ) -> StoredIssuedInvoiceVoid:
        """Hydrate one unused issued-invoice void."""
        cursor.execute(
            """
            SELECT issued_invoice_void_id, tenant_account_id, issued_invoice_id,
                   invoice_draft_id, collection_case_id,
                   issued_invoice_void_contract_version, source_payload_hash,
                   currency_code, voided_amount, remaining_outstanding_amount,
                   issued_invoice_void_status, voided_at
            FROM billing_core.issued_invoice_void
            WHERE issued_invoice_void_id = %s
            """,
            (issued_invoice_void_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(issued_invoice_void_id)
        return self._issued_invoice_void_from_row(row)

    @staticmethod
    def _issued_credit_note_void_from_row(
        row: tuple[Any, ...],
    ) -> StoredIssuedCreditNoteVoid:
        """Decode one normalized unused issued-credit-note void row."""
        return StoredIssuedCreditNoteVoid(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            None if row[5] is None else UUID(str(row[5])),
            row[6],
            row[7],
            row[8],
            parse_exact_decimal(format_exact_decimal(row[9])),
            row[10],
            row[11],
        )

    def _fetch_issued_credit_note_void(
        self, cursor: Any, issued_credit_note_void_id: UUID
    ) -> StoredIssuedCreditNoteVoid:
        """Hydrate one unused issued-credit-note void."""
        cursor.execute(
            """
            SELECT issued_credit_note_void_id, tenant_account_id,
                   issued_credit_note_id, credit_adjustment_id, invoice_draft_id,
                   issued_invoice_id, issued_credit_note_void_contract_version,
                   source_payload_hash, currency_code, voided_amount,
                   issued_credit_note_void_status, voided_at
            FROM billing_core.issued_credit_note_void
            WHERE issued_credit_note_void_id = %s
            """,
            (issued_credit_note_void_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(issued_credit_note_void_id)
        return self._issued_credit_note_void_from_row(row)

    @staticmethod
    def _credit_note_application_from_row(
        row: tuple[Any, ...],
    ) -> StoredCreditNoteApplication:
        """Decode one normalized credit-note application row."""
        return StoredCreditNoteApplication(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            None if row[5] is None else UUID(str(row[5])),
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            parse_exact_decimal(format_exact_decimal(row[11])),
            row[12],
            row[13],
        )

    def _fetch_credit_note_application(
        self, cursor: Any, credit_note_application_id: UUID
    ) -> StoredCreditNoteApplication:
        """Hydrate one credit-note application."""
        cursor.execute(
            """
            SELECT credit_note_application_id, tenant_account_id,
                   issued_credit_note_id, collection_case_id, invoice_draft_id,
                   issued_invoice_id, credit_note_application_contract_version,
                   issued_credit_note_contract_version, source_payload_hash,
                   issued_credit_note_source_payload_hash, currency_code,
                   applied_amount, credit_note_application_status, applied_at
            FROM billing_core.credit_note_application
            WHERE credit_note_application_id = %s
            """,
            (credit_note_application_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(credit_note_application_id)
        return self._credit_note_application_from_row(row)

    @staticmethod
    def _spend_budget_from_row(row: tuple[Any, ...]) -> StoredSpendBudget:
        """Decode one normalized published spend-budget row."""
        return StoredSpendBudget(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            parse_exact_decimal(format_exact_decimal(row[5])),
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
        )

    def _fetch_spend_budget(
        self, cursor: Any, spend_budget_id: UUID
    ) -> StoredSpendBudget:
        """Hydrate one published spend budget."""
        cursor.execute(
            """
            SELECT spend_budget_id, tenant_account_id, billing_account_id,
                   spend_budget_contract_version, currency_code, budget_amount,
                   window_started_at, window_ended_at, source_payload_hash,
                   published_at, spend_budget_status
            FROM billing_core.spend_budget
            WHERE spend_budget_id = %s
            """,
            (spend_budget_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(spend_budget_id)
        return self._spend_budget_from_row(row)

    @staticmethod
    def _collection_write_off_from_row(
        row: tuple[Any, ...],
    ) -> StoredCollectionWriteOff:
        """Decode one normalized collection write-off row."""
        return StoredCollectionWriteOff(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            None if row[4] is None else UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            parse_exact_decimal(format_exact_decimal(row[8])),
            parse_exact_decimal(format_exact_decimal(row[9])),
            row[10],
            row[11],
        )

    def _fetch_collection_write_off(
        self, cursor: Any, collection_write_off_id: UUID
    ) -> StoredCollectionWriteOff:
        """Hydrate one collection write-off."""
        cursor.execute(
            """
            SELECT collection_write_off_id, tenant_account_id, collection_case_id,
                   invoice_draft_id, issued_invoice_id,
                   collection_write_off_contract_version, source_payload_hash,
                   currency_code, write_off_amount, remaining_outstanding_amount,
                   collection_write_off_status, written_off_at
            FROM billing_core.collection_write_off
            WHERE collection_write_off_id = %s
            """,
            (collection_write_off_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(collection_write_off_id)
        return self._collection_write_off_from_row(row)

    @staticmethod
    def _collection_case_settlement_from_row(
        row: tuple[Any, ...],
    ) -> StoredCollectionCaseSettlement:
        """Decode one normalized collection settlement row."""
        return StoredCollectionCaseSettlement(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            None if row[4] is None else UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            parse_exact_decimal(format_exact_decimal(row[8])),
            row[9],
            row[10],
        )

    def _fetch_collection_case_settlement(
        self, cursor: Any, collection_case_settlement_id: UUID
    ) -> StoredCollectionCaseSettlement:
        """Hydrate one collection-case settlement."""
        cursor.execute(
            """
            SELECT collection_case_settlement_id, tenant_account_id,
                   collection_case_id, invoice_draft_id, issued_invoice_id,
                   collection_case_settlement_contract_version, source_payload_hash,
                   currency_code, remaining_outstanding_amount,
                   collection_case_settlement_status, settled_at
            FROM billing_core.collection_case_settlement
            WHERE collection_case_settlement_id = %s
            """,
            (collection_case_settlement_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(collection_case_settlement_id)
        return self._collection_case_settlement_from_row(row)

    @staticmethod
    def _collection_dispute_from_row(row: tuple[Any, ...]) -> StoredCollectionDispute:
        """Decode one normalized collection-dispute row."""
        return StoredCollectionDispute(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            None if row[4] is None else UUID(str(row[4])),
            row[5],
            row[6],
            row[7],
            parse_exact_decimal(format_exact_decimal(row[8])),
            row[9],
            row[10],
            row[11],
        )

    def _fetch_collection_dispute(
        self, cursor: Any, collection_dispute_id: UUID
    ) -> StoredCollectionDispute:
        """Hydrate one collection dispute."""
        cursor.execute(
            """
            SELECT collection_dispute_id, tenant_account_id, collection_case_id,
                   invoice_draft_id, issued_invoice_id,
                   collection_dispute_contract_version, source_payload_hash,
                   currency_code, remaining_outstanding_amount,
                   collection_dispute_status, held_at, released_at
            FROM billing_core.collection_dispute
            WHERE collection_dispute_id = %s
            """,
            (collection_dispute_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(collection_dispute_id)
        return self._collection_dispute_from_row(row)

    @staticmethod
    def _journal_proposal_from_row(
        row: tuple[Any, ...], lines: tuple[StoredJournalProposalLine, ...]
    ) -> StoredJournalProposal:
        """Decode one normalized journal-proposal row and its lines."""
        return StoredJournalProposal(
            journal_proposal_id=UUID(str(row[0])),
            tenant_account_id=UUID(str(row[1])),
            invoice_draft_id=UUID(str(row[2])),
            proposal_contract_version=row[3],
            idempotency_key=row[4],
            legal_entity_reference=row[5],
            intended_book_role_code=row[6],
            transaction_currency=row[7],
            transaction_date=row[8].isoformat()
            if hasattr(row[8], "isoformat")
            else row[8],
            accounting_date=row[9].isoformat()
            if hasattr(row[9], "isoformat")
            else row[9],
            source_payload_hash=row[10],
            proposed_at=row[11],
            proposal_status=row[12],
            source_event_reference=row[13],
            proposal_lines=lines,
            payment_receipt_id=None if row[14] is None else UUID(str(row[14])),
            credit_adjustment_id=None if row[15] is None else UUID(str(row[15])),
            collection_write_off_id=None if row[16] is None else UUID(str(row[16])),
            unapplied_cash_refund_id=None if row[17] is None else UUID(str(row[17])),
            unapplied_cash_id=None if row[18] is None else UUID(str(row[18])),
            unapplied_cash_application_id=None
            if row[19] is None
            else UUID(str(row[19])),
            issued_invoice_void_id=None if row[20] is None else UUID(str(row[20])),
            issued_credit_note_void_id=None if row[21] is None else UUID(str(row[21])),
        )

    def _fetch_journal_proposal(
        self, cursor: Any, journal_proposal_id: UUID
    ) -> StoredJournalProposal:
        """Hydrate one journal proposal and its immutable lines."""
        cursor.execute(
            """
            SELECT journal_proposal_id, tenant_account_id, invoice_draft_id,
                   proposal_contract_version, idempotency_key, legal_entity_reference,
                   intended_book_role_code, transaction_currency, transaction_date,
                   accounting_date, source_payload_hash, proposed_at, proposal_status,
                   source_event_reference, payment_receipt_id, credit_adjustment_id,
                   collection_write_off_id, unapplied_cash_refund_id, unapplied_cash_id,
                   unapplied_cash_application_id, issued_invoice_void_id,
                   issued_credit_note_void_id
            FROM billing_core.journal_proposal
            WHERE journal_proposal_id = %s
            """,
            (journal_proposal_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(journal_proposal_id)
        cursor.execute(
            """
            SELECT journal_proposal_line_id, journal_proposal_id,
                   tenant_account_id, line_number, account_role_code,
                   debit_amount, credit_amount
            FROM billing_core.journal_proposal_line
            WHERE journal_proposal_id = %s
            ORDER BY line_number, journal_proposal_line_id
            """,
            (journal_proposal_id,),
        )
        lines = tuple(
            StoredJournalProposalLine(
                UUID(str(line[0])),
                UUID(str(line[1])),
                UUID(str(line[2])),
                line[3],
                line[4],
                parse_exact_decimal(format_exact_decimal(line[5])),
                parse_exact_decimal(format_exact_decimal(line[6])),
            )
            for line in cursor.fetchall()
        )
        return self._journal_proposal_from_row(row, lines)

    @staticmethod
    def _webhook_subscription_from_row(
        row: tuple[Any, ...],
    ) -> StoredWebhookSubscription:
        """Decode one normalized webhook subscription row."""
        return StoredWebhookSubscription(
            UUID(str(row[0])),
            UUID(str(row[1])),
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
        )

    def _fetch_webhook_subscription(
        self, cursor: Any, webhook_subscription_id: UUID
    ) -> StoredWebhookSubscription:
        """Hydrate one subscription."""
        cursor.execute(
            """
            SELECT webhook_subscription_id, tenant_account_id,
                   webhook_subscription_contract_version, callback_url,
                   event_type_set, webhook_secret_prefix, webhook_secret_hash,
                   subscription_status, issued_at, revoked_at
            FROM billing_core.webhook_subscription
            WHERE webhook_subscription_id = %s
            """,
            (webhook_subscription_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(webhook_subscription_id)
        return self._webhook_subscription_from_row(row)

    def _list_webhook_subscriptions(
        self,
        tenant_account_id: UUID,
        *,
        event_type_code: str | None = None,
    ) -> tuple[StoredWebhookSubscription, ...]:
        """List subscriptions with explicit tenant and optional event predicates."""
        with self._cursor() as cursor:
            if event_type_code is not None:
                cursor.execute(
                    """
                    SELECT webhook_subscription_id
                    FROM billing_core.webhook_subscription
                    WHERE tenant_account_id = %s
                      AND subscription_status = 'active'
                      AND position(',' || %s || ',' in ',' || event_type_set || ',') > 0
                    ORDER BY issued_at, webhook_subscription_id
                    """,
                    (tenant_account_id, event_type_code),
                )
            else:
                cursor.execute(
                    """
                    SELECT webhook_subscription_id
                    FROM billing_core.webhook_subscription
                    WHERE tenant_account_id = %s
                    ORDER BY issued_at, webhook_subscription_id
                    """,
                    (tenant_account_id,),
                )
            return tuple(
                self._fetch_webhook_subscription(cursor, UUID(str(row[0])))
                for row in cursor.fetchall()
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

    @staticmethod
    def _webhook_delivery_attempt_from_row(
        row: tuple[Any, ...],
    ) -> StoredWebhookDeliveryAttempt:
        """Decode one normalized webhook delivery attempt row."""
        return StoredWebhookDeliveryAttempt(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        )

    def _fetch_webhook_delivery_attempt(
        self, cursor: Any, delivery_attempt_id: UUID
    ) -> StoredWebhookDeliveryAttempt:
        """Hydrate one delivery attempt."""
        cursor.execute(
            """
            SELECT delivery_attempt_id, outbox_event_id, webhook_subscription_id,
                   attempt_number, http_status, delivered_at,
                   failure_reason_code, attempted_at
            FROM billing_core.webhook_delivery_attempt
            WHERE delivery_attempt_id = %s
            """,
            (delivery_attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - caller selected an existing row
            raise KeyError(delivery_attempt_id)
        return self._webhook_delivery_attempt_from_row(row)

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
            return tuple(
                self._webhook_outbox_from_row(row) for row in cursor.fetchall()
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
        return CredentialRecord(
            UUID(str(row[0])), UUID(str(row[1])), row[2], row[3], row[4]
        )

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


UsageLedger = MemoryUsageLedger | PostgresUsageLedger
"""Ledger implementations accepted by the HTTP adapter and service wiring.

``MemoryUsageLedger`` stays the deterministic reference and test adapter;
:class:`PostgresUsageLedger` is the durable production system of record.
"""
