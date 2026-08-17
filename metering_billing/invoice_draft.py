"""Invoice-intent drafts produced from already-stored rating runs.

The service is the buyer-facing draft path:

1. Resolve the tenant.
2. Load that tenant's stored ``rating_run``.
3. Copy exact billable line totals into an append-only invoice draft.
4. Replay the same tenant and rating-run identity as the stored draft.

The draft is a commercial document, not a statutory invoice and not revenue
recognition (IFRS Foundation, 2024).  It does not issue, collect, call a
payment provider, or post a journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from metering_billing.errors import InvoiceDraftOutcomeCode, InvoiceDraftRejectionReasonCode
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredInvoiceDraft,
    StoredInvoiceDraftLine,
    StoredRatingRun,
    generate_record_id,
)


Clock = Callable[[], datetime]
INVOICE_DRAFT_CONTRACT_VERSION = 1
INVOICE_DRAFT_STATUS = "draft"


def parse_invoice_amount(value: Any) -> Decimal:
    """Parse an invoice-intent amount as an exact non-negative decimal.

    Binary floating-point values are rejected at this boundary so a draft
    cannot smuggle IEEE inexact money into invoice-intent totals.
    """
    if isinstance(value, Decimal):
        return parse_exact_decimal(format_exact_decimal(value))
    return parse_exact_decimal(value)


@dataclass(frozen=True)
class InvoiceDraftLineResult:
    """One draft line copied from a persisted rating line."""

    line_number: int
    billing_account_reference: str
    meter_code: str
    unit_code: str
    rated_quantity: Decimal
    unit_price_amount: Decimal
    line_total_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the invoice-draft schema."""
        return {
            "line_number": self.line_number,
            "billing_account_reference": self.billing_account_reference,
            "meter_code": self.meter_code,
            "unit_code": self.unit_code,
            "rated_quantity": format_exact_decimal(self.rated_quantity),
            "unit_price_amount": format_exact_decimal(self.unit_price_amount),
            "line_total_amount": format_exact_decimal(self.line_total_amount),
        }


@dataclass(frozen=True)
class InvoiceDraftResult:
    """Buyer-facing result of drafting one tenant rating run."""

    invoice_draft_outcome_code: InvoiceDraftOutcomeCode
    invoice_draft_contract_version: int
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    rating_run_id: UUID | None
    usage_snapshot_hash: str | None
    currency_code: str | None
    invoice_draft_status: str | None
    drafted_total_amount: Decimal | None
    rejection_reason_code: InvoiceDraftRejectionReasonCode | None
    invoice_draft_lines: tuple[InvoiceDraftLineResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the invoice-draft schema."""
        outcome = self.invoice_draft_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, InvoiceDraftOutcomeCode) else str(outcome)
        )
        payload: dict[str, object] = {
            "invoice_draft_contract_version": self.invoice_draft_contract_version,
            "invoice_draft_outcome_code": outcome_text,
        }
        if outcome_text == InvoiceDraftOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else "rating_run_not_found"
            )
            return payload
        if (
            outcome_text != InvoiceDraftOutcomeCode.ACCEPTED
            and outcome_text != InvoiceDraftOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported invoice draft outcome: {outcome_text}")
        payload["invoice_draft_id"] = str(self.invoice_draft_id)
        payload["tenant_reference"] = self.tenant_reference
        payload["rating_run_id"] = str(self.rating_run_id)
        payload["usage_snapshot_hash"] = self.usage_snapshot_hash
        payload["currency_code"] = self.currency_code
        payload["invoice_draft_status"] = self.invoice_draft_status
        payload["drafted_total_amount"] = format_exact_decimal(self.drafted_total_amount)
        payload["invoice_draft_lines"] = [
            line.as_contract_dict() for line in self.invoice_draft_lines
        ]
        return payload


class InvoiceDraftService:
    """Append-only invoice-intent drafter backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def draft_invoice(
        self, tenant_reference: str, rating_run_id: UUID
    ) -> InvoiceDraftResult:
        """Draft invoice intent for one tenant and one stored rating run.

        A replay of the same tenant and rating-run identity returns the stored
        ``invoice_draft_id`` and exact totals.  Another tenant cannot see or
        total that draft.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(InvoiceDraftRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        rating_run = self.ledger.get_rating_run(rating_run_id)
        if rating_run is None or rating_run.tenant_account_id != tenant.tenant_account_id:
            return _rejected(InvoiceDraftRejectionReasonCode.RATING_RUN_NOT_FOUND)

        existing = self.ledger.find_invoice_draft(tenant.tenant_account_id, rating_run.rating_run_id)
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, InvoiceDraftOutcomeCode.DUPLICATE_REPLAY
            )

        invoice_draft_id = generate_record_id()
        stored_lines = _build_draft_lines(invoice_draft_id, rating_run)
        drafted_total_amount = parse_invoice_amount(rating_run.rated_total_amount)
        stored = self.ledger.insert_invoice_draft(
            StoredInvoiceDraft(
                invoice_draft_id=invoice_draft_id,
                tenant_account_id=tenant.tenant_account_id,
                rating_run_id=rating_run.rating_run_id,
                usage_snapshot_hash=rating_run.usage_snapshot_hash,
                currency_code=rating_run.currency_code,
                invoice_draft_status=INVOICE_DRAFT_STATUS,
                drafted_total_amount=drafted_total_amount,
                recorded_at=self._clock(),
                invoice_draft_lines=stored_lines,
            ),
            stored_lines,
        )
        return _from_stored(stored, tenant.tenant_reference, InvoiceDraftOutcomeCode.ACCEPTED)


def _build_draft_lines(
    invoice_draft_id: UUID, rating_run: StoredRatingRun
) -> tuple[StoredInvoiceDraftLine, ...]:
    """Copy rating lines into exact draft lines."""
    return tuple(
        StoredInvoiceDraftLine(
            invoice_draft_line_id=generate_record_id(),
            invoice_draft_id=invoice_draft_id,
            tenant_account_id=rating_run.tenant_account_id,
            billing_account_id=line.billing_account_id,
            billing_account_reference=line.billing_account_reference,
            meter_definition_id=line.meter_definition_id,
            meter_code=line.meter_code,
            unit_code=line.unit_code,
            rated_quantity=parse_invoice_amount(line.rated_quantity),
            unit_price_amount=parse_invoice_amount(line.unit_price_amount),
            line_total_amount=parse_invoice_amount(line.line_total_amount),
            line_number=line.line_number,
        )
        for line in rating_run.rating_lines
    )


def _rejected(reason_code: InvoiceDraftRejectionReasonCode) -> InvoiceDraftResult:
    """Build a rejected result without writing a draft."""
    return InvoiceDraftResult(
        invoice_draft_outcome_code=InvoiceDraftOutcomeCode.REJECTED,
        invoice_draft_contract_version=INVOICE_DRAFT_CONTRACT_VERSION,
        invoice_draft_id=None,
        tenant_reference=None,
        rating_run_id=None,
        usage_snapshot_hash=None,
        currency_code=None,
        invoice_draft_status=None,
        drafted_total_amount=None,
        rejection_reason_code=reason_code,
        invoice_draft_lines=(),
    )


def _from_stored(
    stored: StoredInvoiceDraft,
    tenant_reference: str,
    outcome: InvoiceDraftOutcomeCode,
) -> InvoiceDraftResult:
    """Project a persisted draft into the buyer-facing result."""
    return InvoiceDraftResult(
        invoice_draft_outcome_code=outcome,
        invoice_draft_contract_version=INVOICE_DRAFT_CONTRACT_VERSION,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        rating_run_id=stored.rating_run_id,
        usage_snapshot_hash=stored.usage_snapshot_hash,
        currency_code=stored.currency_code,
        invoice_draft_status=stored.invoice_draft_status,
        drafted_total_amount=stored.drafted_total_amount,
        rejection_reason_code=None,
        invoice_draft_lines=tuple(
            InvoiceDraftLineResult(
                line_number=line.line_number,
                billing_account_reference=line.billing_account_reference,
                meter_code=line.meter_code,
                unit_code=line.unit_code,
                rated_quantity=line.rated_quantity,
                unit_price_amount=line.unit_price_amount,
                line_total_amount=line.line_total_amount,
            )
            for line in stored.invoice_draft_lines
        ),
    )
