"""Immutable commercial invoice snapshots issued from stored invoice drafts.

The service is the buyer-facing issue path:

1. Resolve the tenant and that tenant's stored ``invoice_draft``.
2. Freeze currency, lines, and tax-exclusive/tax/inclusive totals.
3. Persist one append-only ``issued_invoice`` identified by the draft.
4. Replay the same tenant and ``invoice_draft_id`` as the stored snapshot.

The issued document is a commercial artifact, not a statutory invoice, tax
invoice certificate, or AIS posting (IFRS Foundation, 2024).  It does not
invent sequential legal numbering, capture payment, or flip
``proposal_status``.  First successful issue enqueues one existing #24
``invoice.issued`` outbox event; replay of the same issued invoice does
not grow the outbox.
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
    IssuedInvoiceOutcomeCode,
    IssuedInvoiceRejectionReasonCode,
    TimeWindowError,
)
from metering_billing.exact_decimal import (
    format_exact_decimal,
    issued_invoice_amount_exceeds_storage_precision,
    sum_exact_decimals,
)
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredInvoiceDraft,
    StoredIssuedInvoice,
    StoredIssuedInvoiceLine,
    StoredLateAdjustmentInvoiceAdjustment,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_INVOICE_ISSUED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
ISSUED_INVOICE_CONTRACT_VERSION = 2
ISSUED_INVOICE_STATUS = "issued"
MAX_ISSUED_INVOICE_LINES = 10000
OPERATOR_ACTION_COLLECT = "collect"
ZERO = Decimal("0")


def next_operator_action() -> str:
    """Return collect so operators use existing collection or credit flows."""
    return OPERATOR_ACTION_COLLECT


def compute_issued_invoice_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the frozen commercial snapshot."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class IssuedInvoiceLineResult:
    """One issued line copied from a persisted invoice-draft line."""

    line_number: int
    billing_account_reference: str
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal
    line_type: str = "usage"
    late_adjustment_invoice_adjustment_id: UUID | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the issued-invoice schema."""
        payload: dict[str, object] = {
            "line_number": self.line_number,
            "billing_account_reference": self.billing_account_reference,
            "meter_code": self.meter_code,
            "unit_code": self.unit_code,
            "rated_quantity": format_exact_decimal(self.rated_quantity),
            "unit_price_amount": format_exact_decimal(self.unit_price_amount),
            "line_total_amount": _format_signed_decimal(self.line_total_amount),
            "line_type": self.line_type,
        }
        if self.late_adjustment_invoice_adjustment_id is not None:
            payload["late_adjustment_invoice_adjustment_id"] = str(
                self.late_adjustment_invoice_adjustment_id
            )
        return payload


@dataclass(frozen=True)
class IssuedInvoiceResult:
    """Buyer-facing result of issuing one commercial invoice snapshot."""

    issued_invoice_outcome_code: IssuedInvoiceOutcomeCode
    issued_invoice_contract_version: int
    issued_invoice_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    rating_run_id: UUID | None
    usage_snapshot_hash: str | None
    currency_code: str | None
    tax_exclusive_amount: Decimal | None
    tax_amount: Decimal | None
    tax_inclusive_amount: Decimal | None
    issued_invoice_status: str | None
    issued_at: datetime | None
    due_at: datetime | None
    source_payload_hash: str | None
    idempotency_key: str | None
    next_operator_action: str
    rejection_reason_code: IssuedInvoiceRejectionReasonCode | None
    issued_invoice_lines: tuple[IssuedInvoiceLineResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published issued invoice, or a sparse rejected result."""
        outcome = self.issued_invoice_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, IssuedInvoiceOutcomeCode) else str(outcome)
        )
        if outcome_text == IssuedInvoiceOutcomeCode.REJECTED:
            return {
                "issued_invoice_contract_version": self.issued_invoice_contract_version,
                "issued_invoice_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else IssuedInvoiceRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != IssuedInvoiceOutcomeCode.ACCEPTED
            and outcome_text != IssuedInvoiceOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported issued invoice outcome: {outcome_text}")
        payload: dict[str, object] = {
            "issued_invoice_contract_version": self.issued_invoice_contract_version,
            "issued_invoice_outcome_code": outcome_text,
            "issued_invoice_id": str(self.issued_invoice_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "rating_run_id": str(self.rating_run_id),
            "usage_snapshot_hash": self.usage_snapshot_hash,
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_invoice_status": self.issued_invoice_status,
            "issued_at": _format_issued_at(self.issued_at),
            "source_payload_hash": self.source_payload_hash,
            "idempotency_key": self.idempotency_key,
            "next_operator_action": self.next_operator_action,
            "issued_invoice_lines": [
                line.as_contract_dict() for line in self.issued_invoice_lines
            ],
        }
        if self.due_at is not None:
            payload["due_at"] = _format_issued_at(self.due_at)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``invoice.issued`` facts for the #24 envelope.

        The payload is a reference plus hash, not the issued invoice body.
        Lines, billing-account references, meter codes, PAN, secrets, and
        statutory identifiers are omitted.
        """
        if self.issued_invoice_id is None or self.invoice_draft_id is None:
            raise ValueError("rejected issued invoice has no webhook event data")
        payload: dict[str, object] = {
            "issued_invoice_id": str(self.issued_invoice_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "issued_invoice_contract_version": self.issued_invoice_contract_version,
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_invoice_status": self.issued_invoice_status,
            "issued_at": _format_issued_at(self.issued_at),
            "rating_run_id": str(self.rating_run_id),
            "usage_snapshot_hash": self.usage_snapshot_hash,
        }
        if self.due_at is not None:
            payload["due_at"] = _format_issued_at(self.due_at)
        return payload


class IssuedInvoiceService:
    """Append-only issuer of commercial invoice snapshots from stored drafts."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def issue_invoice(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        due_at: object | None = None,
    ) -> IssuedInvoiceResult:
        """Issue one immutable snapshot for a same-tenant stored draft.

        Replay of the same tenant and ``invoice_draft_id`` returns the stored
        ``issued_invoice_id`` and frozen totals.  A later ``due_at`` is ignored.
        First successful issue enqueues one ``invoice.issued`` outbox event.
        Replay of that snapshot does not enqueue a second row.
        """
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._issue_invoice(tenant_reference, invoice_draft_id, due_at)
        with transaction():
            return self._issue_invoice(tenant_reference, invoice_draft_id, due_at)

    def _issue_invoice(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        due_at: object | None,
    ) -> IssuedInvoiceResult:
        """Issue one snapshot inside the caller's transaction boundary."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(IssuedInvoiceRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        parsed_due_at, due_error = _parse_optional_due_at(due_at)
        if due_error is not None:
            return _rejected(due_error)
        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if (
            invoice_draft is None
            or invoice_draft.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(IssuedInvoiceRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        locked_draft = getattr(self.ledger, "lock_invoice_draft", None)
        if locked_draft is not None:
            invoice_draft = locked_draft(
                tenant.tenant_account_id, invoice_draft.invoice_draft_id
            )
            if invoice_draft is None:
                return _rejected(IssuedInvoiceRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        existing = self.ledger.find_issued_invoice(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, IssuedInvoiceOutcomeCode.DUPLICATE_REPLAY
            )
            _enqueue_invoice_issued(self.ledger, tenant.tenant_reference, result)
            return result
        compositions = self.ledger.list_late_adjustment_invoice_adjustments_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )
        draft_billing_accounts = {
            (line.billing_account_id, line.billing_account_reference)
            for line in invoice_draft.invoice_draft_lines
        }
        if compositions and (
            len(draft_billing_accounts) != 1
            or any(
                (composition.billing_account_id, composition.billing_account_reference)
                not in draft_billing_accounts
                for composition in compositions
            )
        ):
            return _rejected(IssuedInvoiceRejectionReasonCode.REQUEST_INVALID)
        if compositions and self.ledger.find_tax_assessment_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        ) is not None:
            return _rejected(
                IssuedInvoiceRejectionReasonCode.LATE_ADJUSTMENT_TAX_REASSESSMENT_REQUIRED
            )
        try:
            exclusive, tax_amount, inclusive = _tax_amounts(
                self.ledger, invoice_draft, compositions
            )
        except ExactDecimalError:
            return _rejected(IssuedInvoiceRejectionReasonCode.REQUEST_INVALID)
        if len(invoice_draft.invoice_draft_lines) + len(compositions) > MAX_ISSUED_INVOICE_LINES:
            return _rejected(IssuedInvoiceRejectionReasonCode.REQUEST_INVALID)
        line_results = _project_draft_lines(
            invoice_draft, compositions
        )
        source_payload_hash = compute_issued_invoice_payload_hash(
            {
                "currency_code": invoice_draft.currency_code,
                "invoice_draft_id": str(invoice_draft.invoice_draft_id),
                "issued_invoice_contract_version": ISSUED_INVOICE_CONTRACT_VERSION,
                "issued_invoice_lines": [line.as_contract_dict() for line in line_results],
                "rating_run_id": str(invoice_draft.rating_run_id),
                "tax_amount": format_exact_decimal(tax_amount),
                "tax_exclusive_amount": format_exact_decimal(exclusive),
                "tax_inclusive_amount": format_exact_decimal(inclusive),
                "usage_snapshot_hash": invoice_draft.usage_snapshot_hash,
            }
        )
        issued_invoice_id = generate_record_id()
        stored_lines = _build_issued_lines(
            issued_invoice_id, invoice_draft, compositions
        )
        stored = self.ledger.insert_issued_invoice(
            StoredIssuedInvoice(
                issued_invoice_id=issued_invoice_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=invoice_draft.invoice_draft_id,
                issued_invoice_contract_version=ISSUED_INVOICE_CONTRACT_VERSION,
                rating_run_id=invoice_draft.rating_run_id,
                usage_snapshot_hash=invoice_draft.usage_snapshot_hash,
                source_payload_hash=source_payload_hash,
                currency_code=invoice_draft.currency_code,
                tax_exclusive_amount=exclusive,
                tax_amount=tax_amount,
                tax_inclusive_amount=inclusive,
                issued_invoice_status=ISSUED_INVOICE_STATUS,
                issued_at=self._clock(),
                due_at=parsed_due_at,
                issued_invoice_lines=stored_lines,
            ),
            stored_lines,
        )
        result = _from_stored(
            stored, tenant.tenant_reference, IssuedInvoiceOutcomeCode.ACCEPTED
        )
        _enqueue_invoice_issued(self.ledger, tenant.tenant_reference, result)
        return result


def _tax_amounts(
    ledger: MemoryUsageLedger,
    invoice_draft: StoredInvoiceDraft,
    compositions: tuple[StoredLateAdjustmentInvoiceAdjustment, ...] = (),
) -> tuple[Decimal, Decimal, Decimal]:
    """Return exclusive, tax, and inclusive amounts frozen from draft or tax."""
    assessment = ledger.find_tax_assessment_for_draft(
        invoice_draft.tenant_account_id, invoice_draft.invoice_draft_id
    )
    if assessment is None:
        exclusive = parse_invoice_amount(invoice_draft.drafted_total_amount)
        exclusive = sum_exact_decimals(
            exclusive, *(composition.adjustment_amount for composition in compositions)
        )
        if exclusive <= ZERO:
            raise ExactDecimalError("late adjustment total must be positive")
        if issued_invoice_amount_exceeds_storage_precision(exclusive):
            raise ExactDecimalError("late adjustment total exceeds storage precision")
        return exclusive, ZERO, exclusive
    exclusive = parse_invoice_amount(assessment.tax_exclusive_amount)
    tax_amount = parse_invoice_amount(assessment.tax_amount)
    inclusive = parse_invoice_amount(assessment.tax_inclusive_amount)
    if sum_exact_decimals(exclusive, tax_amount) != inclusive:
        raise ExactDecimalError("tax snapshot does not sum to inclusive")
    if any(
        issued_invoice_amount_exceeds_storage_precision(amount)
        for amount in (exclusive, tax_amount, inclusive)
    ):
        raise ExactDecimalError("tax snapshot exceeds storage precision")
    return exclusive, tax_amount, inclusive


def _project_draft_lines(
    invoice_draft: StoredInvoiceDraft,
    compositions: tuple[StoredLateAdjustmentInvoiceAdjustment, ...] = (),
) -> tuple[IssuedInvoiceLineResult, ...]:
    """Project draft lines into exact issued-line results for hashing."""
    draft_lines = tuple(
        IssuedInvoiceLineResult(
            line_number=line.line_number,
            billing_account_reference=line.billing_account_reference,
            meter_code=line.meter_code,
            unit_code=line.unit_code,
            rated_quantity=parse_invoice_amount(line.rated_quantity),
            unit_price_amount=parse_invoice_amount(line.unit_price_amount),
            line_total_amount=parse_invoice_amount(line.line_total_amount),
        )
        for line in invoice_draft.invoice_draft_lines
    )
    adjustment_lines = tuple(
        IssuedInvoiceLineResult(
            line_number=len(draft_lines) + offset,
            billing_account_reference=composition.billing_account_reference
            or "urn:cwl:invalid",
            meter_code="late_adjustment",
            unit_code="adjustment",
            rated_quantity=Decimal("1"),
            unit_price_amount=composition.adjustment_amount.copy_abs(),
            line_total_amount=composition.adjustment_amount,
            line_type="late_adjustment",
            late_adjustment_invoice_adjustment_id=(
                composition.late_adjustment_invoice_adjustment_id
            ),
        )
        for offset, composition in enumerate(compositions, start=1)
    )
    return draft_lines + adjustment_lines


def _build_issued_lines(
    issued_invoice_id: UUID,
    invoice_draft: StoredInvoiceDraft,
    compositions: tuple[StoredLateAdjustmentInvoiceAdjustment, ...] = (),
) -> tuple[StoredIssuedInvoiceLine, ...]:
    """Copy draft lines into exact issued lines."""
    draft_lines = tuple(
        StoredIssuedInvoiceLine(
            issued_invoice_line_id=generate_record_id(),
            issued_invoice_id=issued_invoice_id,
            tenant_account_id=invoice_draft.tenant_account_id,
            line_number=line.line_number,
            billing_account_reference=line.billing_account_reference,
            meter_code=line.meter_code,
            unit_code=line.unit_code,
            rated_quantity=parse_invoice_amount(line.rated_quantity),
            unit_price_amount=parse_invoice_amount(line.unit_price_amount),
            line_total_amount=parse_invoice_amount(line.line_total_amount),
        )
        for line in invoice_draft.invoice_draft_lines
    )
    adjustment_lines = tuple(
        StoredIssuedInvoiceLine(
            issued_invoice_line_id=generate_record_id(),
            issued_invoice_id=issued_invoice_id,
            tenant_account_id=invoice_draft.tenant_account_id,
            line_number=len(draft_lines) + offset,
            billing_account_reference=composition.billing_account_reference
            or "urn:cwl:invalid",
            meter_code="late_adjustment",
            unit_code="adjustment",
            rated_quantity=Decimal("1"),
            unit_price_amount=composition.adjustment_amount.copy_abs(),
            line_total_amount=composition.adjustment_amount,
            line_type="late_adjustment",
            late_adjustment_invoice_adjustment_id=(
                composition.late_adjustment_invoice_adjustment_id
            ),
        )
        for offset, composition in enumerate(compositions, start=1)
    )
    return draft_lines + adjustment_lines


def _format_signed_decimal(amount: Decimal) -> str:
    """Render an exact finite signed commercial line amount."""
    if not isinstance(amount, Decimal) or amount.is_nan() or amount.is_infinite():
        raise ExactDecimalError("line amount must be a finite decimal")
    return format(amount, "f")


def _parse_optional_due_at(
    due_at: object | None,
) -> tuple[datetime | None, IssuedInvoiceRejectionReasonCode | None]:
    """Accept a timezone-aware due instant or reject an unreadable value."""
    if due_at is None or due_at == "":
        return None, None
    if isinstance(due_at, datetime):
        if due_at.tzinfo is None:
            return None, IssuedInvoiceRejectionReasonCode.REQUEST_INVALID
        return due_at, None
    try:
        return parse_iso8601_datetime(due_at), None
    except (TimeWindowError, TypeError, ValueError):
        return None, IssuedInvoiceRejectionReasonCode.REQUEST_INVALID


def _enqueue_invoice_issued(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: IssuedInvoiceResult,
) -> None:
    """Append one ``invoice.issued`` outbox row for a stored snapshot.

    Replay of the same tenant, event type, ``issued_invoice_id``, and
    payload hash returns the stored row.  A crash after insert and before
    enqueue is healed by the next issue replay.
    """
    assert result.issued_invoice_id is not None
    assert result.issued_at is not None
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_INVOICE_ISSUED,
        result.issued_invoice_id,
        result.as_webhook_event_data(),
        result.issued_at,
    )


def _rejected(reason: IssuedInvoiceRejectionReasonCode) -> IssuedInvoiceResult:
    """Return a sparse rejected issue result without writing a snapshot."""
    return IssuedInvoiceResult(
        issued_invoice_outcome_code=IssuedInvoiceOutcomeCode.REJECTED,
        issued_invoice_contract_version=ISSUED_INVOICE_CONTRACT_VERSION,
        issued_invoice_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        rating_run_id=None,
        usage_snapshot_hash=None,
        currency_code=None,
        tax_exclusive_amount=None,
        tax_amount=None,
        tax_inclusive_amount=None,
        issued_invoice_status=None,
        issued_at=None,
        due_at=None,
        source_payload_hash=None,
        idempotency_key=None,
        next_operator_action=OPERATOR_ACTION_COLLECT,
        rejection_reason_code=reason,
        issued_invoice_lines=(),
    )


def _from_stored(
    stored: StoredIssuedInvoice,
    tenant_reference: str,
    outcome: IssuedInvoiceOutcomeCode,
) -> IssuedInvoiceResult:
    """Project a persisted snapshot into the buyer-facing result."""
    return IssuedInvoiceResult(
        issued_invoice_outcome_code=outcome,
        issued_invoice_contract_version=stored.issued_invoice_contract_version,
        issued_invoice_id=stored.issued_invoice_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        rating_run_id=stored.rating_run_id,
        usage_snapshot_hash=stored.usage_snapshot_hash,
        currency_code=stored.currency_code,
        tax_exclusive_amount=stored.tax_exclusive_amount,
        tax_amount=stored.tax_amount,
        tax_inclusive_amount=stored.tax_inclusive_amount,
        issued_invoice_status=stored.issued_invoice_status,
        issued_at=stored.issued_at,
        due_at=stored.due_at,
        source_payload_hash=stored.source_payload_hash,
        idempotency_key=(
            f"{tenant_reference}:issued_invoice:{stored.issued_invoice_id}:"
            f"{stored.source_payload_hash}:v{stored.issued_invoice_contract_version}"
        ),
        next_operator_action=OPERATOR_ACTION_COLLECT,
        rejection_reason_code=None,
        issued_invoice_lines=tuple(
            IssuedInvoiceLineResult(
                line_number=line.line_number,
                billing_account_reference=line.billing_account_reference,
                meter_code=line.meter_code,
                unit_code=line.unit_code,
                rated_quantity=line.rated_quantity,
                unit_price_amount=line.unit_price_amount,
                line_total_amount=line.line_total_amount,
                line_type=line.line_type,
                late_adjustment_invoice_adjustment_id=(
                    line.late_adjustment_invoice_adjustment_id
                ),
            )
            for line in stored.issued_invoice_lines
        ),
    )


def _format_issued_at(issued_at: datetime | None) -> str:
    """Render an issue timestamp as a timezone-aware ISO 8601 instant."""
    assert issued_at is not None
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
