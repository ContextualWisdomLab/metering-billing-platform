"""Commercial tax assessment on a stored invoice draft.

The service is the buyer-facing tax path:

1. Resolve the tenant and a persisted same-tenant ``tax_rate_version``.
2. Load that tenant's stored ``invoice_draft``.
3. Keep ``tax_exclusive_amount`` as the drafted commercial subtotal.
4. Round ``tax_exclusive_amount * tax_rate`` half-even to the currency minor
   units and persist an append-only ``tax_assessment``.

ISO 4217 minor units are a closed table: ``0`` for JPY and KRW, ``2`` for the
listed two-decimal currencies.  An unknown currency fails closed
(International Organization for Standardization, 2015).  Collection must not
already be open.  Tax-payable unwind on credit is a later slice
(IFRS Foundation, 2024; OECD, 2017).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    TaxAssessmentOutcomeCode,
    TaxAssessmentQueryError,
    TaxAssessmentRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredInvoiceDraft,
    StoredTaxAssessment,
    StoredTaxRateVersion,
    generate_record_id,
)


Clock = Callable[[], datetime]
TAX_ASSESSMENT_CONTRACT_VERSION = 1
NEXT_OPERATOR_ACTION = (
    "Publish a tax rate, assess the draft, then propose the journal and let AIS pull."
)

# ISO 4217 minor-unit exponents this slice will assess.  Unknown codes reject.
CURRENCY_MINOR_UNITS: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "AED": 2,
    "AUD": 2,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CNY": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "ILS": 2,
    "INR": 2,
    "MXN": 2,
    "MYR": 2,
    "NOK": 2,
    "NZD": 2,
    "PHP": 2,
    "PLN": 2,
    "SAR": 2,
    "SEK": 2,
    "SGD": 2,
    "THB": 2,
    "TWD": 2,
    "USD": 2,
    "ZAR": 2,
}


class CurrencyExponentError(ValueError):
    """Raised when a currency has no documented ISO 4217 minor-unit exponent."""


def currency_minor_units(currency_code: str) -> int:
    """Return the documented ISO 4217 minor-unit exponent, or fail closed."""
    if currency_code not in CURRENCY_MINOR_UNITS:
        raise CurrencyExponentError(currency_code)
    return CURRENCY_MINOR_UNITS[currency_code]


def round_tax_amount(product: Decimal, currency_code: str) -> Decimal:
    """Round a tax product half-even to the currency minor units."""
    exponent = currency_minor_units(currency_code)
    quantum = Decimal("1").scaleb(-exponent)
    return product.quantize(quantum, rounding=ROUND_HALF_EVEN)


def compute_tax_assessment_payload_hash(payload: dict[str, object]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical assessment payload."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class TaxAssessmentResult:
    """Buyer-facing result of assessing tax on one invoice draft."""

    tax_assessment_outcome_code: TaxAssessmentOutcomeCode
    tax_assessment_contract_version: int
    tax_assessment_id: UUID | None
    tenant_reference: str | None
    invoice_draft_id: UUID | None
    tax_rate_version_id: UUID | None
    tax_rate_version: int | None
    tax_code: str | None
    tax_rate: Decimal | None
    currency_code: str | None
    tax_exclusive_amount: Decimal | None
    tax_amount: Decimal | None
    tax_inclusive_amount: Decimal | None
    source_payload_hash: str | None
    assessed_at: datetime | None
    next_operator_action: str
    rejection_reason_code: TaxAssessmentRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published assessment, or a sparse rejected result."""
        outcome = self.tax_assessment_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, TaxAssessmentOutcomeCode) else str(outcome)
        )
        if outcome_text == TaxAssessmentOutcomeCode.REJECTED:
            return {
                "tax_assessment_contract_version": self.tax_assessment_contract_version,
                "tax_assessment_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else TaxAssessmentRejectionReasonCode.TAX_ASSESSMENT_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != TaxAssessmentOutcomeCode.ACCEPTED
            and outcome_text != TaxAssessmentOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported tax assessment outcome: {outcome_text}")
        if (
            self.tax_assessment_id is None
            or self.invoice_draft_id is None
            or self.tax_rate_version_id is None
            or self.assessed_at is None
            or self.tax_exclusive_amount is None
            or self.tax_amount is None
            or self.tax_inclusive_amount is None
            or self.tax_rate is None
        ):
            raise ValueError("accepted tax assessments must include identity and amounts")
        return {
            "tax_assessment_contract_version": self.tax_assessment_contract_version,
            "tax_assessment_outcome_code": outcome_text,
            "tax_assessment_id": str(self.tax_assessment_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "tax_rate_version_id": str(self.tax_rate_version_id),
            "tax_rate_version": self.tax_rate_version,
            "tax_code": self.tax_code,
            "tax_rate": format_exact_decimal(self.tax_rate),
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "source_payload_hash": self.source_payload_hash,
            "assessed_at": _format_assessed_at(self.assessed_at),
            "next_operator_action": self.next_operator_action,
        }


class TaxAssessmentService:
    """Append-only tax assessor backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def assess_tax(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        tax_rate_version: UUID | int,
    ) -> TaxAssessmentResult:
        """Assess tax inside the repository transaction boundary."""
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._assess_tax(tenant_reference, invoice_draft_id, tax_rate_version)
        with transaction():
            return self._assess_tax(tenant_reference, invoice_draft_id, tax_rate_version)

    def _assess_tax(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        tax_rate_version: UUID | int,
    ) -> TaxAssessmentResult:
        """Assess a persisted tax-rate version against one invoice draft.

        A replay of the same tenant, draft, tax-rate version, and source-payload
        hash returns the stored ``tax_assessment_id``.  A collection case
        already open for the draft is ``tax_after_collection_opened``.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(TaxAssessmentRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(TaxAssessmentRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if not isinstance(invoice_draft_id, UUID):
            return _rejected(TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        locked_draft = getattr(self.ledger, "lock_invoice_draft", None)
        if locked_draft is not None:
            invoice_draft = locked_draft(tenant.tenant_account_id, invoice_draft.invoice_draft_id)
            if invoice_draft is None:
                return _rejected(TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        rate_version = _resolve_rate_version(
            self.ledger, tenant.tenant_account_id, tax_rate_version
        )
        if rate_version is None:
            return _rejected(TaxAssessmentRejectionReasonCode.TAX_RATE_NOT_FOUND)
        find_collection_case = getattr(self.ledger, "find_collection_case", None)
        collection_case = (
            None
            if find_collection_case is None
            else find_collection_case(tenant.tenant_account_id, invoice_draft.invoice_draft_id)
        )
        if collection_case is not None:
            return _rejected(TaxAssessmentRejectionReasonCode.TAX_AFTER_COLLECTION_OPENED)
        try:
            exclusive_amount = parse_invoice_amount(invoice_draft.drafted_total_amount)
        except ExactDecimalError:
            return _rejected(TaxAssessmentRejectionReasonCode.DRAFT_TOTAL_INVALID)
        if exclusive_amount <= 0:
            return _rejected(TaxAssessmentRejectionReasonCode.DRAFT_TOTAL_INVALID)
        try:
            tax_amount = round_tax_amount(
                exclusive_amount * rate_version.tax_rate, invoice_draft.currency_code
            )
        except CurrencyExponentError:
            return _rejected(TaxAssessmentRejectionReasonCode.CURRENCY_EXPONENT_UNKNOWN)
        inclusive_amount = exclusive_amount + tax_amount
        source_payload_hash = compute_tax_assessment_payload_hash(
            _canonical_assessment_snapshot(
                invoice_draft, rate_version, exclusive_amount, tax_amount, inclusive_amount
            )
        )
        existing = self.ledger.find_tax_assessment(
            tenant.tenant_account_id,
            invoice_draft.invoice_draft_id,
            rate_version.tax_rate_version_id,
            source_payload_hash,
            TAX_ASSESSMENT_CONTRACT_VERSION,
        )
        if existing is not None:
            return _from_stored(
                existing,
                tenant.tenant_reference,
                TaxAssessmentOutcomeCode.DUPLICATE_REPLAY,
            )
        if self.ledger.list_late_adjustment_invoice_adjustments_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        ):
            return _rejected(TaxAssessmentRejectionReasonCode.INVOICE_DRAFT_HAS_LATE_ADJUSTMENT)
        stored = self.ledger.insert_tax_assessment(
            StoredTaxAssessment(
                tax_assessment_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=invoice_draft.invoice_draft_id,
                tax_rate_version_id=rate_version.tax_rate_version_id,
                tax_assessment_contract_version=TAX_ASSESSMENT_CONTRACT_VERSION,
                tax_code=rate_version.tax_code,
                tax_rate=rate_version.tax_rate,
                currency_code=invoice_draft.currency_code,
                tax_exclusive_amount=exclusive_amount,
                tax_amount=tax_amount,
                tax_inclusive_amount=inclusive_amount,
                source_payload_hash=source_payload_hash,
                assessed_at=self._clock(),
                tax_rate_version_number=rate_version.version_number,
            )
        )
        return _from_stored(stored, tenant.tenant_reference, TaxAssessmentOutcomeCode.ACCEPTED)

    def get_tax_assessment(
        self, tenant_reference: str, tax_assessment_id: UUID
    ) -> TaxAssessmentResult:
        """Return one same-tenant stored assessment, or fail closed."""
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise TaxAssessmentQueryError("tenant_not_found")
        if not isinstance(tax_assessment_id, UUID):
            raise TaxAssessmentQueryError("tax_assessment_not_found")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise TaxAssessmentQueryError("tenant_not_found")
        assert tenant is not None
        stored = self.ledger.get_tax_assessment(tax_assessment_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise TaxAssessmentQueryError("tax_assessment_not_found")
        return _from_stored(stored, tenant.tenant_reference, TaxAssessmentOutcomeCode.ACCEPTED)


def _canonical_assessment_snapshot(
    invoice_draft: StoredInvoiceDraft,
    rate_version: StoredTaxRateVersion,
    exclusive_amount: Decimal,
    tax_amount: Decimal,
    inclusive_amount: Decimal,
) -> dict[str, object]:
    """Return draft, version, and rounded amounts for assessment identity."""
    return {
        "invoice_draft_id": str(invoice_draft.invoice_draft_id),
        "tax_rate_version_id": str(rate_version.tax_rate_version_id),
        "tax_code": rate_version.tax_code,
        "tax_rate": format_exact_decimal(rate_version.tax_rate),
        "currency_code": invoice_draft.currency_code,
        "tax_exclusive_amount": format_exact_decimal(exclusive_amount),
        "tax_amount": format_exact_decimal(tax_amount),
        "tax_inclusive_amount": format_exact_decimal(inclusive_amount),
        "tax_assessment_contract_version": TAX_ASSESSMENT_CONTRACT_VERSION,
    }


def _resolve_rate_version(
    ledger: MemoryUsageLedger,
    tenant_account_id: UUID,
    tax_rate_version: UUID | int,
) -> StoredTaxRateVersion | None:
    """Resolve a published tax-rate version by UUID or unique version number."""
    if isinstance(tax_rate_version, UUID):
        stored = ledger.get_tax_rate_version(tax_rate_version)
        if stored is None or stored.tenant_account_id != tenant_account_id:
            return None
        return stored
    if isinstance(tax_rate_version, bool) or not isinstance(tax_rate_version, int):
        return None
    return ledger.find_tax_rate_version(tenant_account_id, tax_rate_version)


def _rejected(reason: TaxAssessmentRejectionReasonCode) -> TaxAssessmentResult:
    """Return a sparse rejected assessment result."""
    return TaxAssessmentResult(
        tax_assessment_outcome_code=TaxAssessmentOutcomeCode.REJECTED,
        tax_assessment_contract_version=TAX_ASSESSMENT_CONTRACT_VERSION,
        tax_assessment_id=None,
        tenant_reference=None,
        invoice_draft_id=None,
        tax_rate_version_id=None,
        tax_rate_version=None,
        tax_code=None,
        tax_rate=None,
        currency_code=None,
        tax_exclusive_amount=None,
        tax_amount=None,
        tax_inclusive_amount=None,
        source_payload_hash=None,
        assessed_at=None,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=reason,
    )


def _from_stored(
    stored: StoredTaxAssessment,
    tenant_reference: str,
    outcome: TaxAssessmentOutcomeCode,
) -> TaxAssessmentResult:
    """Project a persisted assessment into the buyer-facing result."""
    version = stored.tax_rate_version_number
    return TaxAssessmentResult(
        tax_assessment_outcome_code=outcome,
        tax_assessment_contract_version=stored.tax_assessment_contract_version,
        tax_assessment_id=stored.tax_assessment_id,
        tenant_reference=tenant_reference,
        invoice_draft_id=stored.invoice_draft_id,
        tax_rate_version_id=stored.tax_rate_version_id,
        tax_rate_version=version,
        tax_code=stored.tax_code,
        tax_rate=stored.tax_rate,
        currency_code=stored.currency_code,
        tax_exclusive_amount=stored.tax_exclusive_amount,
        tax_amount=stored.tax_amount,
        tax_inclusive_amount=stored.tax_inclusive_amount,
        source_payload_hash=stored.source_payload_hash,
        assessed_at=stored.assessed_at,
        next_operator_action=NEXT_OPERATOR_ACTION,
        rejection_reason_code=None,
    )


def _format_assessed_at(assessed_at: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for the assessment instant."""
    return assessed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
