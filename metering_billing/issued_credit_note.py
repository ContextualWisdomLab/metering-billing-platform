"""Immutable commercial credit-note snapshots issued from stored credits.

The service is the buyer-facing issue path:

1. Resolve the tenant and that tenant's stored ``credit_adjustment``.
2. Freeze currency and tax-exclusive/tax/inclusive credit amounts.
3. Persist one append-only ``issued_credit_note`` identified by the credit.
4. Replay the same tenant and ``credit_adjustment_id`` as the stored snapshot.

The issued document is a commercial artifact, not a statutory credit note,
tax credit certificate, or AIS posting (IFRS Foundation, 2024).  It does
not invent sequential legal numbering, capture payment, enqueue a webhook,
or flip ``proposal_status``.  The validated credit journal remains
available for AIS to pull.
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
    IssuedCreditNoteOutcomeCode,
    IssuedCreditNoteRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCreditAdjustment,
    StoredIssuedCreditNote,
    generate_record_id,
)


Clock = Callable[[], datetime]
ISSUED_CREDIT_NOTE_CONTRACT_VERSION = 1
ISSUED_CREDIT_NOTE_STATUS = "issued"
OPERATOR_ACTION_WAIT = "wait"


def next_operator_action() -> str:
    """Return wait so operators leave the validated journal for AIS."""
    return OPERATOR_ACTION_WAIT


def compute_issued_credit_note_payload_hash(payload: Mapping[str, Any]) -> str:
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
class IssuedCreditNoteResult:
    """Buyer-facing result of issuing one commercial credit-note snapshot."""

    issued_credit_note_outcome_code: IssuedCreditNoteOutcomeCode
    issued_credit_note_contract_version: int
    issued_credit_note_id: UUID | None
    credit_adjustment_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    tax_exclusive_amount: Decimal | None
    tax_amount: Decimal | None
    tax_inclusive_amount: Decimal | None
    issued_credit_note_status: str | None
    issued_at: datetime | None
    source_payload_hash: str | None
    credit_adjustment_source_payload_hash: str | None
    credit_adjustment_contract_version: int
    credit_reason_code: str | None
    idempotency_key: str | None
    next_operator_action: str
    rejection_reason_code: IssuedCreditNoteRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published issued credit note, or a sparse rejected result."""
        outcome = self.issued_credit_note_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, IssuedCreditNoteOutcomeCode) else str(outcome)
        )
        if outcome_text == IssuedCreditNoteOutcomeCode.REJECTED:
            return {
                "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
                "issued_credit_note_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else IssuedCreditNoteRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != IssuedCreditNoteOutcomeCode.ACCEPTED
            and outcome_text != IssuedCreditNoteOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported issued credit note outcome: {outcome_text}")
        payload: dict[str, object] = {
            "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
            "issued_credit_note_outcome_code": outcome_text,
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "tenant_reference": self.tenant_reference,
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "tax_exclusive_amount": format_exact_decimal(self.tax_exclusive_amount),
            "tax_amount": format_exact_decimal(self.tax_amount),
            "tax_inclusive_amount": format_exact_decimal(self.tax_inclusive_amount),
            "issued_credit_note_status": self.issued_credit_note_status,
            "issued_at": _format_issued_at(self.issued_at),
            "source_payload_hash": self.source_payload_hash,
            "credit_adjustment_source_payload_hash": self.credit_adjustment_source_payload_hash,
            "credit_adjustment_contract_version": self.credit_adjustment_contract_version,
            "credit_reason_code": self.credit_reason_code,
            "idempotency_key": self.idempotency_key,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class IssuedCreditNoteService:
    """Append-only issuer of commercial credit-note snapshots from stored credits."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def issue_credit_note(
        self,
        tenant_reference: str,
        credit_adjustment_id: UUID,
    ) -> IssuedCreditNoteResult:
        """Issue one immutable snapshot for a same-tenant stored credit.

        Replay of the same tenant and ``credit_adjustment_id`` returns the
        stored ``issued_credit_note_id`` and frozen totals.  The path does
        not enqueue a webhook or change the credit journal.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(IssuedCreditNoteRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        credit = self.ledger.get_credit_adjustment(credit_adjustment_id)
        if credit is None or credit.tenant_account_id != tenant.tenant_account_id:
            return _rejected(IssuedCreditNoteRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND)
        existing = self.ledger.find_issued_credit_note(
            tenant.tenant_account_id, credit.credit_adjustment_id
        )
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, IssuedCreditNoteOutcomeCode.DUPLICATE_REPLAY
            )
        issued_invoice = self.ledger.find_issued_invoice(
            tenant.tenant_account_id, credit.invoice_draft_id
        )
        issued_invoice_id = (
            issued_invoice.issued_invoice_id if issued_invoice is not None else None
        )
        source_payload_hash = compute_issued_credit_note_payload_hash(
            _canonical_snapshot(credit, issued_invoice_id)
        )
        stored = self.ledger.insert_issued_credit_note(
            StoredIssuedCreditNote(
                issued_credit_note_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                credit_adjustment_id=credit.credit_adjustment_id,
                invoice_draft_id=credit.invoice_draft_id,
                issued_invoice_id=issued_invoice_id,
                issued_credit_note_contract_version=ISSUED_CREDIT_NOTE_CONTRACT_VERSION,
                credit_adjustment_contract_version=credit.credit_adjustment_contract_version,
                credit_reason_code=credit.credit_reason_code,
                credit_adjustment_source_payload_hash=credit.source_payload_hash,
                source_payload_hash=source_payload_hash,
                currency_code=credit.currency_code,
                tax_exclusive_amount=credit.tax_exclusive_amount,
                tax_amount=credit.tax_amount,
                tax_inclusive_amount=credit.credit_amount,
                issued_credit_note_status=ISSUED_CREDIT_NOTE_STATUS,
                issued_at=self._clock(),
            )
        )
        return _from_stored(
            stored, tenant.tenant_reference, IssuedCreditNoteOutcomeCode.ACCEPTED
        )


def _canonical_snapshot(
    credit: StoredCreditAdjustment, issued_invoice_id: UUID | None
) -> dict[str, object]:
    """Return credit identity, versions, currency, and frozen amounts."""
    payload: dict[str, object] = {
        "credit_adjustment_id": str(credit.credit_adjustment_id),
        "credit_adjustment_contract_version": credit.credit_adjustment_contract_version,
        "credit_adjustment_source_payload_hash": credit.source_payload_hash,
        "currency_code": credit.currency_code,
        "invoice_draft_id": str(credit.invoice_draft_id),
        "issued_credit_note_contract_version": ISSUED_CREDIT_NOTE_CONTRACT_VERSION,
        "tax_amount": format_exact_decimal(credit.tax_amount),
        "tax_exclusive_amount": format_exact_decimal(credit.tax_exclusive_amount),
        "tax_inclusive_amount": format_exact_decimal(credit.credit_amount),
    }
    if issued_invoice_id is not None:
        payload["issued_invoice_id"] = str(issued_invoice_id)
    return payload


def _rejected(reason: IssuedCreditNoteRejectionReasonCode) -> IssuedCreditNoteResult:
    """Return a sparse rejected issue result without writing a snapshot."""
    return IssuedCreditNoteResult(
        issued_credit_note_outcome_code=IssuedCreditNoteOutcomeCode.REJECTED,
        issued_credit_note_contract_version=ISSUED_CREDIT_NOTE_CONTRACT_VERSION,
        issued_credit_note_id=None,
        credit_adjustment_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        tax_exclusive_amount=None,
        tax_amount=None,
        tax_inclusive_amount=None,
        issued_credit_note_status=None,
        issued_at=None,
        source_payload_hash=None,
        credit_adjustment_source_payload_hash=None,
        credit_adjustment_contract_version=ISSUED_CREDIT_NOTE_CONTRACT_VERSION,
        credit_reason_code=None,
        idempotency_key=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason,
    )


def _from_stored(
    stored: StoredIssuedCreditNote,
    tenant_reference: str,
    outcome: IssuedCreditNoteOutcomeCode,
) -> IssuedCreditNoteResult:
    """Project a persisted snapshot into the buyer-facing result."""
    return IssuedCreditNoteResult(
        issued_credit_note_outcome_code=outcome,
        issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
        issued_credit_note_id=stored.issued_credit_note_id,
        credit_adjustment_id=stored.credit_adjustment_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        tax_exclusive_amount=stored.tax_exclusive_amount,
        tax_amount=stored.tax_amount,
        tax_inclusive_amount=stored.tax_inclusive_amount,
        issued_credit_note_status=stored.issued_credit_note_status,
        issued_at=stored.issued_at,
        source_payload_hash=stored.source_payload_hash,
        credit_adjustment_source_payload_hash=stored.credit_adjustment_source_payload_hash,
        credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
        credit_reason_code=stored.credit_reason_code,
        idempotency_key=(
            f"{tenant_reference}:issued_credit_note:{stored.issued_credit_note_id}:"
            f"{stored.source_payload_hash}:v{stored.issued_credit_note_contract_version}"
        ),
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_issued_at(issued_at: datetime | None) -> str:
    """Render an issue timestamp as a timezone-aware ISO 8601 instant."""
    assert issued_at is not None
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
