"""Tenant-scoped versioned tax-rate catalog.

The service is the buyer-facing tax-rate path:

1. Resolve the tenant.
2. Accept a closed ``tax_code`` and an exact ``tax_rate`` in ``[0, 1]``.
3. Persist one ``tax_rate_schedule`` and an immutable ``tax_rate_version``.
4. Replay the same tenant, tax code, rate, and contract version.

A published version is never edited.  Publishing a distinct rate on the same
code creates the next version.  Assessment later resolves that persisted
version; this service does not invent a hidden default rate (OECD, 2017).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    TaxRateOutcomeCode,
    TaxRateQueryError,
    TaxRateRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredTaxRateSchedule,
    StoredTaxRateVersion,
    generate_record_id,
)


Clock = Callable[[], datetime]
TAX_RATE_CONTRACT_VERSION = 1
TAX_CODES = frozenset({"vat", "gst", "sales_tax"})
NEXT_OPERATOR_ACTION = (
    "Publish a tax rate, assess the draft, then propose the journal and let AIS pull."
)


def parse_tax_rate(value: Any) -> Decimal:
    """Parse a flat tax rate as an exact decimal in ``[0, 1]``.

    Binary floating-point values and percent integers such as ``10`` are
    rejected so a catalog cannot smuggle IEEE inexact money into later
    assessment.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ExactDecimalError("tax rate must be an exact decimal")
    if isinstance(value, int) and value not in (0, 1):
        raise ExactDecimalError("tax rate must be an exact decimal in [0, 1]")
    if isinstance(value, Decimal):
        parsed = parse_exact_decimal(format_exact_decimal(value))
    elif isinstance(value, int):
        parsed = parse_exact_decimal(str(value))
    else:
        parsed = parse_exact_decimal(value)
    if parsed > 1:
        raise ExactDecimalError("tax rate must be an exact decimal in [0, 1]")
    return parsed


def compute_tax_rate_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical published rate."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class TaxRateResult:
    """Buyer-facing result of publishing or reading one tax-rate version."""

    tax_rate_outcome_code: TaxRateOutcomeCode
    tax_rate_contract_version: int
    tax_rate_schedule_id: UUID | None
    tax_rate_version_id: UUID | None
    tenant_reference: str | None
    tax_code: str | None
    tax_rate_version: int | None
    tax_rate: Decimal | None
    source_payload_hash: str | None
    published_at: datetime | None
    next_operator_action: str
    rejection_reason_code: TaxRateRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published rate, or a sparse rejected operational result."""
        outcome = self.tax_rate_outcome_code
        outcome_text = outcome.value if isinstance(outcome, TaxRateOutcomeCode) else str(outcome)
        if outcome_text == TaxRateOutcomeCode.REJECTED:
            return {
                "tax_rate_contract_version": self.tax_rate_contract_version,
                "tax_rate_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else TaxRateRejectionReasonCode.TAX_RATE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != TaxRateOutcomeCode.ACCEPTED
            and outcome_text != TaxRateOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported tax rate outcome: {outcome_text}")
        if (
            self.tax_rate_schedule_id is None
            or self.tax_rate_version_id is None
            or self.published_at is None
            or self.tax_rate_version is None
            or self.tax_rate is None
        ):
            raise ValueError("accepted tax rates must include identity and version")
        return {
            "tax_rate_contract_version": self.tax_rate_contract_version,
            "tax_rate_outcome_code": outcome_text,
            "tax_rate_schedule_id": str(self.tax_rate_schedule_id),
            "tax_rate_version_id": str(self.tax_rate_version_id),
            "tenant_reference": self.tenant_reference,
            "tax_code": self.tax_code,
            "tax_rate_version": self.tax_rate_version,
            "tax_rate": format_exact_decimal(self.tax_rate),
            "source_payload_hash": self.source_payload_hash,
            "published_at": _format_published_at(self.published_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class TaxRateListPage:
    """Tenant-scoped list of tax-rate schedule headers."""

    tenant_reference: str
    tax_rates: tuple[dict[str, object], ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON list published for GET /v1/tax-rates."""
        return {
            "tenant_reference": self.tenant_reference,
            "tax_rates": list(self.tax_rates),
        }


class TaxRateService:
    """Append-only tenant-scoped tax-rate publisher backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def publish_tax_rate(
        self,
        tenant_reference: str,
        tax_code: str,
        tax_rate: Any,
    ) -> TaxRateResult:
        """Publish one tax rate inside the repository transaction boundary."""
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._publish_tax_rate(tenant_reference, tax_code, tax_rate)
        with transaction():
            return self._publish_tax_rate(tenant_reference, tax_code, tax_rate)

    def _publish_tax_rate(
        self,
        tenant_reference: str,
        tax_code: str,
        tax_rate: Any,
    ) -> TaxRateResult:
        """Publish one immutable tax-rate version for a tenant.

        A replay of the same tenant, tax code, exact rate, and contract
        version returns the stored ``tax_rate_version``.  A later distinct
        rate increments the version.  Assessment next uses that persisted
        version.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(TaxRateRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(TaxRateRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if not isinstance(tax_code, str) or tax_code not in TAX_CODES:
            return _rejected(TaxRateRejectionReasonCode.TAX_CODE_INVALID)
        try:
            parsed_rate = parse_tax_rate(tax_rate)
        except ExactDecimalError:
            return _rejected(TaxRateRejectionReasonCode.TAX_RATE_INVALID)
        source_payload_hash = compute_tax_rate_payload_hash(
            {
                "tax_code": tax_code,
                "tax_rate": format_exact_decimal(parsed_rate),
                "tax_rate_contract_version": TAX_RATE_CONTRACT_VERSION,
            }
        )
        existing_schedule = self.ledger.find_tax_rate_schedule(
            tenant.tenant_account_id, tax_code
        )
        if existing_schedule is not None:
            existing_version = self.ledger.find_tax_rate_version_by_identity(
                tenant.tenant_account_id,
                existing_schedule.tax_rate_schedule_id,
                source_payload_hash,
                TAX_RATE_CONTRACT_VERSION,
            )
            if existing_version is not None:
                return _from_stored(
                    existing_schedule,
                    existing_version,
                    tenant.tenant_reference,
                    TaxRateOutcomeCode.DUPLICATE_REPLAY,
                )
        stored_schedule = self.ledger.insert_tax_rate_schedule(
            StoredTaxRateSchedule(
                tax_rate_schedule_id=(
                    existing_schedule.tax_rate_schedule_id
                    if existing_schedule is not None
                    else generate_record_id()
                ),
                tenant_account_id=tenant.tenant_account_id,
                tax_code=tax_code,
                created_at=self._clock(),
            )
        )
        version_number = self.ledger.next_tax_rate_version_number(
            tenant.tenant_account_id, stored_schedule.tax_rate_schedule_id
        )
        stored_version = self.ledger.insert_tax_rate_version(
            StoredTaxRateVersion(
                tax_rate_version_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                tax_rate_schedule_id=stored_schedule.tax_rate_schedule_id,
                version_number=version_number,
                tax_rate_contract_version=TAX_RATE_CONTRACT_VERSION,
                tax_code=tax_code,
                tax_rate=parsed_rate,
                source_payload_hash=source_payload_hash,
                published_at=self._clock(),
            )
        )
        return _from_stored(
            stored_schedule,
            stored_version,
            tenant.tenant_reference,
            TaxRateOutcomeCode.ACCEPTED,
        )

    def list_tax_rates(self, tenant_reference: str) -> TaxRateListPage:
        """Return tenant-scoped tax-rate headers, or fail closed."""
        tenant = _require_tenant(self.ledger, tenant_reference)
        rates = []
        for schedule in self.ledger.list_tax_rate_schedules(tenant.tenant_account_id):
            latest = _latest_version(
                self.ledger, tenant.tenant_account_id, schedule.tax_rate_schedule_id
            )
            rates.append(
                {
                    "tax_rate_schedule_id": str(schedule.tax_rate_schedule_id),
                    "tax_code": schedule.tax_code,
                    "latest_tax_rate_version": (
                        latest.version_number if latest is not None else None
                    ),
                    "tax_rate": (
                        format_exact_decimal(latest.tax_rate) if latest is not None else None
                    ),
                }
            )
        return TaxRateListPage(tenant_reference=tenant.tenant_reference, tax_rates=tuple(rates))

    def get_tax_rate_version(
        self, tenant_reference: str, tax_rate_version: UUID | int
    ) -> TaxRateResult:
        """Return one same-tenant published version, or fail closed.

        ``tax_rate_version`` may be the internal version identifier or the
        integer version number.  A missing or cross-tenant identifier is
        indistinguishable.
        """
        tenant = _require_tenant(self.ledger, tenant_reference)
        stored = _resolve_version(self.ledger, tenant.tenant_account_id, tax_rate_version)
        if stored is None:
            raise TaxRateQueryError("tax_rate_not_found")
        schedule = self.ledger.get_tax_rate_schedule(stored.tax_rate_schedule_id)
        if schedule is None or schedule.tenant_account_id != tenant.tenant_account_id:
            raise TaxRateQueryError("tax_rate_not_found")
        return _from_stored(schedule, stored, tenant.tenant_reference, TaxRateOutcomeCode.ACCEPTED)


def _require_tenant(ledger: MemoryUsageLedger, tenant_reference: str):
    """Return a stored tenant or raise a tenant-scoped query error."""
    if not isinstance(tenant_reference, str) or not tenant_reference:
        raise TaxRateQueryError("tenant_not_found")
    tenant, tenant_error = ledger.resolve_tenant(tenant_reference)
    if tenant_error is not None:
        raise TaxRateQueryError("tenant_not_found")
    assert tenant is not None
    return tenant


def _latest_version(
    ledger: MemoryUsageLedger, tenant_account_id: UUID, tax_rate_schedule_id: UUID
) -> StoredTaxRateVersion | None:
    """Return the highest published version number for one schedule, if any."""
    versions = ledger.list_tax_rate_versions(tenant_account_id, tax_rate_schedule_id)
    if not versions:
        return None
    return max(versions, key=lambda version: version.version_number)


def _resolve_version(
    ledger: MemoryUsageLedger,
    tenant_account_id: UUID,
    tax_rate_version: UUID | int,
) -> StoredTaxRateVersion | None:
    """Resolve a version by internal identifier or tenant-scoped number."""
    if isinstance(tax_rate_version, UUID):
        stored = ledger.get_tax_rate_version(tax_rate_version)
        if stored is None or stored.tenant_account_id != tenant_account_id:
            return None
        return stored
    if isinstance(tax_rate_version, bool) or not isinstance(tax_rate_version, int):
        return None
    return ledger.find_tax_rate_version(tenant_account_id, tax_rate_version)


def _rejected(reason: TaxRateRejectionReasonCode) -> TaxRateResult:
    """Return a sparse rejected catalog result."""
    return TaxRateResult(
        tax_rate_outcome_code=TaxRateOutcomeCode.REJECTED,
        tax_rate_contract_version=TAX_RATE_CONTRACT_VERSION,
        tax_rate_schedule_id=None,
        tax_rate_version_id=None,
        tenant_reference=None,
        tax_code=None,
        tax_rate_version=None,
        tax_rate=None,
        source_payload_hash=None,
        published_at=None,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=reason,
    )


def _from_stored(
    schedule: StoredTaxRateSchedule,
    version: StoredTaxRateVersion,
    tenant_reference: str,
    outcome: TaxRateOutcomeCode,
) -> TaxRateResult:
    """Project a persisted schedule and version into the buyer-facing result."""
    return TaxRateResult(
        tax_rate_outcome_code=outcome,
        tax_rate_contract_version=version.tax_rate_contract_version,
        tax_rate_schedule_id=schedule.tax_rate_schedule_id,
        tax_rate_version_id=version.tax_rate_version_id,
        tenant_reference=tenant_reference,
        tax_code=schedule.tax_code,
        tax_rate_version=version.version_number,
        tax_rate=version.tax_rate,
        source_payload_hash=version.source_payload_hash,
        published_at=version.published_at,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=None,
    )


def _format_published_at(published_at: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for the publish instant."""
    return published_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
