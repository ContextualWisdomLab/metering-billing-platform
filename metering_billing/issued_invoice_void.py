"""Immutable commercial void of one issued invoice.

The service is the buyer-facing void path:

1. Resolve the tenant and same-tenant ``issued_invoice``.
2. Refuse when the related collection case has cash, credit apply,
   unapplied-cash apply, or a write-off.
3. Persist one append-only ``issued_invoice_void`` per issued invoice.
4. Close an unused open or dunning case as ``voided`` at exact-zero remaining.

Replay of the same tenant and ``issued_invoice_id`` returns the stored void
and does not insert a second row.  Already-``voided`` cases stay as-is.
A crash after insert and before ``mark_collection_case_voided`` is healed
when the stored void's case is still ``open`` or ``dunning`` and remaining
still equals the issued amount.  First successful void enqueues one
``invoice.voided`` outbox event; replay is ``duplicate_replay`` with
crash-heal enqueue.  The path does not emit a journal, refund, write-off,
settlement, or AIS call.  The issued snapshot stays ``issued``; history is
the void row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.collection_case import (
    COLLECTION_CASE_DISPUTED_STATUS,
    COLLECTION_CASE_DUNNING_STATUS,
    COLLECTION_CASE_OPEN_STATUS,
    COLLECTION_CASE_SETTLED_STATUS,
    COLLECTION_CASE_VOIDED_STATUS,
)
from metering_billing.errors import (
    IssuedInvoiceVoidOutcomeCode,
    IssuedInvoiceVoidRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredIssuedInvoice,
    StoredIssuedInvoiceVoid,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_INVOICE_VOIDED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
ISSUED_INVOICE_VOID_CONTRACT_VERSION = 1
ISSUED_INVOICE_VOID_STATUS = "recorded"
OPERATOR_ACTION_WAIT = "wait"
ZERO = Decimal("0")


def compute_issued_invoice_void_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical void identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class IssuedInvoiceVoidResult:
    """Buyer-facing result of voiding one issued invoice."""

    issued_invoice_void_outcome_code: IssuedInvoiceVoidOutcomeCode
    issued_invoice_void_contract_version: int
    issued_invoice_void_id: UUID | None
    issued_invoice_id: UUID | None
    invoice_draft_id: UUID | None
    collection_case_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    voided_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    issued_invoice_void_status: str | None
    collection_case_status: str | None
    voided_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: IssuedInvoiceVoidRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published void, or a sparse rejected result."""
        outcome = self.issued_invoice_void_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, IssuedInvoiceVoidOutcomeCode)
            else str(outcome)
        )
        if outcome_text == IssuedInvoiceVoidOutcomeCode.REJECTED:
            return {
                "issued_invoice_void_contract_version": (
                    self.issued_invoice_void_contract_version
                ),
                "issued_invoice_void_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != IssuedInvoiceVoidOutcomeCode.ACCEPTED
            and outcome_text != IssuedInvoiceVoidOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported issued-invoice void outcome: {outcome_text}")
        payload: dict[str, object] = {
            "issued_invoice_void_contract_version": self.issued_invoice_void_contract_version,
            "issued_invoice_void_outcome_code": outcome_text,
            "issued_invoice_void_id": str(self.issued_invoice_void_id),
            "tenant_reference": self.tenant_reference,
            "issued_invoice_id": str(self.issued_invoice_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "voided_amount": format_exact_decimal(self.voided_amount),
            "issued_invoice_void_status": self.issued_invoice_void_status,
            "voided_at": _format_voided_at(self.voided_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.collection_case_id is not None:
            payload["collection_case_id"] = str(self.collection_case_id)
        if self.remaining_outstanding_amount is not None:
            payload["remaining_outstanding_amount"] = format_exact_decimal(
                self.remaining_outstanding_amount
            )
        if self.collection_case_status is not None:
            payload["collection_case_status"] = self.collection_case_status
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``invoice.voided`` facts for the #24 envelope.

        The payload is a reference plus hash, not a remaining snapshot or
        collection status.  PII, PAN, secrets, and statutory identifiers
        are omitted.
        """
        if (
            self.issued_invoice_void_id is None
            or self.issued_invoice_id is None
            or self.invoice_draft_id is None
        ):
            raise ValueError("rejected issued-invoice void has no webhook event data")
        if self.voided_at is None:
            raise ValueError("accepted issued-invoice voids must include voided_at")
        payload: dict[str, object] = {
            "issued_invoice_void_id": str(self.issued_invoice_void_id),
            "issued_invoice_id": str(self.issued_invoice_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "issued_invoice_void_contract_version": (
                self.issued_invoice_void_contract_version
            ),
            "currency_code": self.currency_code,
            "voided_amount": format_exact_decimal(self.voided_amount),
            "issued_invoice_void_status": self.issued_invoice_void_status,
            "voided_at": _format_voided_at(self.voided_at),
        }
        if self.collection_case_id is not None:
            payload["collection_case_id"] = str(self.collection_case_id)
        return payload


class IssuedInvoiceVoidService:
    """Append-only writer of one commercial issued-invoice void."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def void_issued_invoice(
        self,
        tenant_reference: str,
        issued_invoice_id: UUID,
        currency_code: str | None = None,
    ) -> IssuedInvoiceVoidResult:
        """Void one same-tenant issued invoice when its case is unused.

        Replay of the same tenant and ``issued_invoice_id`` returns the
        stored ``issued_invoice_void_id`` and does not insert a second
        row.  Already-``voided`` cases stay as-is.  A recorded void whose
        case is still ``open`` or ``dunning`` at the issued remaining is
        closed as ``voided`` at exact zero.  Another tenant cannot see
        or void that invoice.  The issued snapshot stays ``issued``.
        First successful void enqueues one ``invoice.voided`` outbox
        event.  Replay of that void does not enqueue a second row.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(IssuedInvoiceVoidRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_issued_invoice_void(
            tenant.tenant_account_id, issued_invoice_id
        )
        if existing is not None:
            current_case = None
            if existing.collection_case_id is not None:
                current_case = self.ledger.get_collection_case(existing.collection_case_id)
                if current_case is None:
                    return _rejected(
                        IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND
                    )
            current_case = _heal_open_case_after_recorded_void(
                self.ledger, existing, current_case
            )
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                IssuedInvoiceVoidOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_invoice_voided(self.ledger, tenant.tenant_reference, result)
            return result
        issued_invoice = self.ledger.get_issued_invoice(issued_invoice_id)
        if (
            issued_invoice is None
            or issued_invoice.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND)
        if currency_code is not None and currency_code != issued_invoice.currency_code:
            return _rejected(IssuedInvoiceVoidRejectionReasonCode.CURRENCY_MISMATCH)
        collection_case = self.ledger.find_collection_case(
            tenant.tenant_account_id, issued_invoice.invoice_draft_id
        )
        blocked = _blocking_collection_reason(self.ledger, collection_case)
        if blocked is not None:
            return _rejected(blocked)
        source_payload_hash = compute_issued_invoice_void_payload_hash(
            _canonical_void_snapshot(issued_invoice)
        )
        candidate = StoredIssuedInvoiceVoid(
            issued_invoice_void_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            issued_invoice_id=issued_invoice.issued_invoice_id,
            invoice_draft_id=issued_invoice.invoice_draft_id,
            collection_case_id=(
                collection_case.collection_case_id if collection_case is not None else None
            ),
            issued_invoice_void_contract_version=ISSUED_INVOICE_VOID_CONTRACT_VERSION,
            source_payload_hash=source_payload_hash,
            currency_code=issued_invoice.currency_code,
            voided_amount=issued_invoice.tax_inclusive_amount,
            remaining_outstanding_amount=ZERO,
            issued_invoice_void_status=ISSUED_INVOICE_VOID_STATUS,
            voided_at=self._clock(),
        )
        stored = self.ledger.insert_issued_invoice_void(candidate)
        if stored.issued_invoice_void_id != candidate.issued_invoice_void_id:
            current_case = None
            if stored.collection_case_id is not None:
                current_case = self.ledger.get_collection_case(stored.collection_case_id)
                if current_case is None:
                    return _rejected(
                        IssuedInvoiceVoidRejectionReasonCode.ISSUED_INVOICE_NOT_FOUND
                    )
            current_case = _heal_open_case_after_recorded_void(
                self.ledger, stored, current_case
            )
            result = _from_stored(
                stored,
                current_case,
                tenant.tenant_reference,
                IssuedInvoiceVoidOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_invoice_voided(self.ledger, tenant.tenant_reference, result)
            return result
        updated_case = None
        if collection_case is not None:
            updated_case = self.ledger.mark_collection_case_voided(
                collection_case.collection_case_id,
                issued_invoice.tax_inclusive_amount,
            )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            IssuedInvoiceVoidOutcomeCode.ACCEPTED,
        )
        _enqueue_invoice_voided(self.ledger, tenant.tenant_reference, result)
        return result


def _heal_open_case_after_recorded_void(
    ledger: MemoryUsageLedger,
    stored: StoredIssuedInvoiceVoid,
    collection_case: StoredCollectionCase | None,
) -> StoredCollectionCase | None:
    """Close an unused open or dunning case left open after a recorded void.

    Already-``voided`` cases stay as-is.  Replay does not reuse
    ``settled``.  Only remaining that still equals the issued voided
    amount is closed at exact zero.
    """
    if collection_case is None:
        return None
    if collection_case.collection_case_status == COLLECTION_CASE_VOIDED_STATUS:
        return collection_case
    if collection_case.collection_case_status not in {
        COLLECTION_CASE_OPEN_STATUS,
        COLLECTION_CASE_DUNNING_STATUS,
    }:
        return collection_case
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = ZERO
    if remaining != stored.voided_amount:
        return collection_case
    return ledger.mark_collection_case_voided(
        collection_case.collection_case_id,
        stored.voided_amount,
    )


def _enqueue_invoice_voided(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: IssuedInvoiceVoidResult,
) -> None:
    """Append one ``invoice.voided`` outbox row for a stored void.

    Replay of the same tenant, event type, ``issued_invoice_void_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next void replay.
    """
    if result.issued_invoice_void_id is None or result.voided_at is None:
        raise ValueError(
            "accepted issued-invoice voids must include identity and voided_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_INVOICE_VOIDED,
        result.issued_invoice_void_id,
        result.as_webhook_event_data(),
        result.voided_at,
    )


def _blocking_collection_reason(
    ledger: MemoryUsageLedger, collection_case: StoredCollectionCase | None
) -> IssuedInvoiceVoidRejectionReasonCode | None:
    """Return why an existing case cannot be voided, or ``None`` when unused."""
    if collection_case is None:
        return None
    if any(
        receipt.collection_case_id == collection_case.collection_case_id
        for receipt in ledger.list_payment_receipts(collection_case.tenant_account_id)
    ):
        return IssuedInvoiceVoidRejectionReasonCode.PAYMENT_RECEIPT_EXISTS
    if any(
        application.collection_case_id == collection_case.collection_case_id
        for application in ledger.list_credit_note_applications_for_tenant(
            collection_case.tenant_account_id
        )
    ):
        return IssuedInvoiceVoidRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED
    if (
        ledger.find_collection_write_off(
            collection_case.tenant_account_id, collection_case.collection_case_id
        )
        is not None
    ):
        return IssuedInvoiceVoidRejectionReasonCode.COLLECTION_WRITE_OFF_EXISTS
    if any(
        application.collection_case_id == collection_case.collection_case_id
        for application in ledger.list_unapplied_cash_applications_for_tenant(
            collection_case.tenant_account_id
        )
    ):
        return IssuedInvoiceVoidRejectionReasonCode.UNAPPLIED_CASH_ALREADY_APPLIED
    if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
        return IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED
    if collection_case.collection_case_status == COLLECTION_CASE_VOIDED_STATUS:
        return IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED
    if collection_case.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
        return IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_DISPUTED
    if collection_case.collection_case_status not in {
        COLLECTION_CASE_OPEN_STATUS,
        COLLECTION_CASE_DUNNING_STATUS,
    }:
        return IssuedInvoiceVoidRejectionReasonCode.COLLECTION_CASE_SETTLED
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = ZERO
    issued = ledger.find_issued_invoice(
        collection_case.tenant_account_id, collection_case.invoice_draft_id
    )
    if issued is None or remaining != issued.tax_inclusive_amount:
        return IssuedInvoiceVoidRejectionReasonCode.OUTSTANDING_MISMATCH
    return None


def _canonical_void_snapshot(issued_invoice: StoredIssuedInvoice) -> dict[str, object]:
    """Return invoice, draft, currency, inclusive amount, and version."""
    return {
        "issued_invoice_id": str(issued_invoice.issued_invoice_id),
        "invoice_draft_id": str(issued_invoice.invoice_draft_id),
        "currency_code": issued_invoice.currency_code,
        "voided_amount": format_exact_decimal(issued_invoice.tax_inclusive_amount),
        "issued_invoice_void_contract_version": ISSUED_INVOICE_VOID_CONTRACT_VERSION,
    }


def _rejected(
    reason_code: IssuedInvoiceVoidRejectionReasonCode,
) -> IssuedInvoiceVoidResult:
    """Build a rejected result without writing a void or changing outstanding."""
    return IssuedInvoiceVoidResult(
        issued_invoice_void_outcome_code=IssuedInvoiceVoidOutcomeCode.REJECTED,
        issued_invoice_void_contract_version=ISSUED_INVOICE_VOID_CONTRACT_VERSION,
        issued_invoice_void_id=None,
        issued_invoice_id=None,
        invoice_draft_id=None,
        collection_case_id=None,
        tenant_reference=None,
        currency_code=None,
        voided_amount=None,
        remaining_outstanding_amount=None,
        issued_invoice_void_status=None,
        collection_case_status=None,
        voided_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredIssuedInvoiceVoid,
    collection_case: StoredCollectionCase | None,
    tenant_reference: str,
    outcome: IssuedInvoiceVoidOutcomeCode,
) -> IssuedInvoiceVoidResult:
    """Project a persisted void and the current case into the result."""
    remaining = None
    collection_case_status = None
    collection_case_id = stored.collection_case_id
    if collection_case is not None:
        remaining = collection_case.outstanding_amount
        if remaining == 0:
            remaining = ZERO
        collection_case_status = collection_case.collection_case_status
        collection_case_id = collection_case.collection_case_id
    return IssuedInvoiceVoidResult(
        issued_invoice_void_outcome_code=outcome,
        issued_invoice_void_contract_version=stored.issued_invoice_void_contract_version,
        issued_invoice_void_id=stored.issued_invoice_void_id,
        issued_invoice_id=stored.issued_invoice_id,
        invoice_draft_id=stored.invoice_draft_id,
        collection_case_id=collection_case_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        voided_amount=stored.voided_amount,
        remaining_outstanding_amount=remaining,
        issued_invoice_void_status=stored.issued_invoice_void_status,
        collection_case_status=collection_case_status,
        voided_at=stored.voided_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_voided_at(voided_at: datetime | None) -> str:
    """Render ``voided_at`` as a timezone-aware ISO 8601 instant."""
    if voided_at is None:
        raise ValueError("accepted issued-invoice voids must include voided_at")
    return voided_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
