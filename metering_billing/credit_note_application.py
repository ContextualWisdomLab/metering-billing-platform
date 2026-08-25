"""Apply one issued credit note onto one open collection case.

The service is the buyer-facing apply path:

1. Resolve the tenant, issued credit note, and open collection case.
2. Require the same invoice and currency.
3. Reduce ``collection_outstanding`` by the exact issued inclusive amount.
4. Persist one append-only ``credit_note_application`` per issued note.

Replay of the same tenant and ``issued_credit_note_id`` returns the stored
application and never double-reduces.  A voided unused note fail-closes
as ``issued_credit_note_voided``.  First successful apply enqueues
one existing ``credit_note.applied`` outbox event; replay of the same
application does not enqueue a second row.  The path does not emit a
journal, unwind tax, capture payment, call AIS, or invent a settlement.
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
    COLLECTION_CASE_SETTLED_STATUS,
)
from metering_billing.errors import (
    CreditNoteApplicationOutcomeCode,
    CreditNoteApplicationRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCreditNoteApplication,
    StoredIssuedCreditNote,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_CREDIT_NOTE_APPLIED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
CREDIT_NOTE_APPLICATION_CONTRACT_VERSION = 1
CREDIT_NOTE_APPLICATION_STATUS = "applied"
OPERATOR_ACTION_COLLECT = "collect"
OPERATOR_ACTION_WAIT = "wait"


def compute_application_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical apply identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CreditNoteApplicationResult:
    """Buyer-facing result of applying one issued credit note to a case."""

    credit_note_application_outcome_code: CreditNoteApplicationOutcomeCode
    credit_note_application_contract_version: int
    credit_note_application_id: UUID | None
    issued_credit_note_id: UUID | None
    collection_case_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    applied_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    credit_note_application_status: str | None
    collection_case_status: str | None
    applied_at: datetime | None
    source_payload_hash: str | None
    issued_credit_note_source_payload_hash: str | None
    issued_credit_note_contract_version: int | None
    next_operator_action: str
    rejection_reason_code: CreditNoteApplicationRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published application, or a sparse rejected result."""
        outcome = self.credit_note_application_outcome_code
        outcome_text = outcome.value if isinstance(outcome, CreditNoteApplicationOutcomeCode) else str(outcome)
        if outcome_text == CreditNoteApplicationOutcomeCode.REJECTED:
            return {
                "credit_note_application_contract_version": (
                    self.credit_note_application_contract_version
                ),
                "credit_note_application_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CreditNoteApplicationOutcomeCode.ACCEPTED
            and outcome_text != CreditNoteApplicationOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported credit note application outcome: {outcome_text}")
        payload: dict[str, object] = {
            "credit_note_application_contract_version": (
                self.credit_note_application_contract_version
            ),
            "credit_note_application_outcome_code": outcome_text,
            "credit_note_application_id": str(self.credit_note_application_id),
            "tenant_reference": self.tenant_reference,
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "applied_amount": format_exact_decimal(self.applied_amount),
            "remaining_outstanding_amount": format_exact_decimal(
                self.remaining_outstanding_amount
            ),
            "credit_note_application_status": self.credit_note_application_status,
            "collection_case_status": self.collection_case_status,
            "applied_at": _format_applied_at(self.applied_at),
            "source_payload_hash": self.source_payload_hash,
            "issued_credit_note_source_payload_hash": (
                self.issued_credit_note_source_payload_hash
            ),
            "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``credit_note.applied`` facts for the #24 envelope.

        The payload is a reference plus hash, not a payment receipt or
        settlement.  PII, PAN, secrets, and statutory identifiers are omitted.
        Remaining outstanding is not stored on the application row, so it is
        omitted to keep the outbox payload hash stable across later case
        mutations.
        """
        if self.credit_note_application_id is None or self.issued_credit_note_id is None:
            raise ValueError("rejected credit note application has no webhook event data")
        if self.applied_at is None:
            raise ValueError("accepted credit note applications must include applied_at")
        payload: dict[str, object] = {
            "credit_note_application_id": str(self.credit_note_application_id),
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "collection_case_id": str(self.collection_case_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "credit_note_application_contract_version": (
                self.credit_note_application_contract_version
            ),
            "issued_credit_note_contract_version": self.issued_credit_note_contract_version,
            "issued_credit_note_source_payload_hash": (
                self.issued_credit_note_source_payload_hash
            ),
            "currency_code": self.currency_code,
            "applied_amount": format_exact_decimal(self.applied_amount),
            "credit_note_application_status": self.credit_note_application_status,
            "applied_at": _format_applied_at(self.applied_at),
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class CreditNoteApplicationService:
    """Append-only applier of issued credit notes onto collection cases."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def apply_credit_note(
        self,
        tenant_reference: str,
        issued_credit_note_id: UUID,
        collection_case_id: UUID,
    ) -> CreditNoteApplicationResult:
        """Apply one same-tenant issued credit note to one open collection case.

        Replay of the same tenant and ``issued_credit_note_id`` returns the
        stored ``credit_note_application_id`` and does not reduce outstanding
        again.  First successful apply enqueues one ``credit_note.applied``
        outbox event.  Replay of that application does not enqueue a second
        row.  Another tenant cannot see or apply that note.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CreditNoteApplicationRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        issued = self.ledger.get_issued_credit_note(issued_credit_note_id)
        if issued is None or issued.tenant_account_id != tenant.tenant_account_id:
            return _rejected(CreditNoteApplicationRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND)
        existing = self.ledger.find_credit_note_application(
            tenant.tenant_account_id, issued.issued_credit_note_id
        )
        if existing is not None:
            current_case = self.ledger.get_collection_case(existing.collection_case_id)
            if current_case is None:
                return _rejected(CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)
            result = _from_stored(
                existing,
                current_case,
                tenant.tenant_reference,
                CreditNoteApplicationOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_credit_note_applied(self.ledger, tenant.tenant_reference, result)
            return result
        if (
            self.ledger.find_issued_credit_note_void(
                tenant.tenant_account_id, issued.issued_credit_note_id
            )
            is not None
        ):
            return _rejected(
                CreditNoteApplicationRejectionReasonCode.ISSUED_CREDIT_NOTE_VOIDED
            )
        collection_case = self.ledger.get_collection_case(collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_NOT_FOUND)
        invoice_error = _invoice_mismatch(self.ledger, issued, collection_case)
        if invoice_error is not None:
            return _rejected(invoice_error)
        if issued.currency_code != collection_case.currency_code:
            return _rejected(CreditNoteApplicationRejectionReasonCode.CURRENCY_MISMATCH)
        if collection_case.collection_case_status == COLLECTION_CASE_SETTLED_STATUS:
            return _rejected(CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_SETTLED)
        if collection_case.collection_case_status == COLLECTION_CASE_DISPUTED_STATUS:
            return _rejected(CreditNoteApplicationRejectionReasonCode.COLLECTION_CASE_DISPUTED)
        applied_amount = issued.tax_inclusive_amount
        if applied_amount > collection_case.outstanding_amount:
            return _rejected(CreditNoteApplicationRejectionReasonCode.CREDIT_EXCEEDS_OUTSTANDING)
        source_payload_hash = compute_application_payload_hash(
            _canonical_application_snapshot(issued, collection_case)
        )
        stored = self.ledger.insert_credit_note_application(
            StoredCreditNoteApplication(
                credit_note_application_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                issued_credit_note_id=issued.issued_credit_note_id,
                collection_case_id=collection_case.collection_case_id,
                invoice_draft_id=issued.invoice_draft_id,
                issued_invoice_id=issued.issued_invoice_id,
                credit_note_application_contract_version=CREDIT_NOTE_APPLICATION_CONTRACT_VERSION,
                issued_credit_note_contract_version=issued.issued_credit_note_contract_version,
                source_payload_hash=source_payload_hash,
                issued_credit_note_source_payload_hash=issued.source_payload_hash,
                currency_code=issued.currency_code,
                applied_amount=applied_amount,
                credit_note_application_status=CREDIT_NOTE_APPLICATION_STATUS,
                applied_at=self._clock(),
            )
        )
        updated_case = self.ledger.apply_collection_settlement(
            collection_case.collection_case_id, applied_amount
        )
        result = _from_stored(
            stored,
            updated_case,
            tenant.tenant_reference,
            CreditNoteApplicationOutcomeCode.ACCEPTED,
        )
        _enqueue_credit_note_applied(self.ledger, tenant.tenant_reference, result)
        return result


def _canonical_application_snapshot(
    issued: StoredIssuedCreditNote, collection_case: StoredCollectionCase
) -> dict[str, object]:
    """Return note, case, invoice, currency, amount, and contract versions."""
    payload: dict[str, object] = {
        "issued_credit_note_id": str(issued.issued_credit_note_id),
        "collection_case_id": str(collection_case.collection_case_id),
        "invoice_draft_id": str(issued.invoice_draft_id),
        "currency_code": issued.currency_code,
        "applied_amount": format_exact_decimal(issued.tax_inclusive_amount),
        "issued_credit_note_contract_version": issued.issued_credit_note_contract_version,
        "credit_note_application_contract_version": CREDIT_NOTE_APPLICATION_CONTRACT_VERSION,
    }
    if issued.issued_invoice_id is not None:
        payload["issued_invoice_id"] = str(issued.issued_invoice_id)
    return payload


def _enqueue_credit_note_applied(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: CreditNoteApplicationResult,
) -> None:
    """Append one ``credit_note.applied`` outbox row for a stored application.

    Replay of the same tenant, event type, ``credit_note_application_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next apply replay.
    """
    if result.credit_note_application_id is None or result.applied_at is None:
        raise ValueError(
            "accepted credit note applications must include identity and applied_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_CREDIT_NOTE_APPLIED,
        result.credit_note_application_id,
        result.as_webhook_event_data(),
        result.applied_at,
    )


def _invoice_mismatch(
    ledger: MemoryUsageLedger,
    issued: StoredIssuedCreditNote,
    collection_case: StoredCollectionCase,
) -> CreditNoteApplicationRejectionReasonCode | None:
    """Reject when the credit's draft or issued invoice is not the case invoice."""
    if issued.invoice_draft_id != collection_case.invoice_draft_id:
        return CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH
    if issued.issued_invoice_id is None:
        return None
    case_invoice = ledger.find_issued_invoice(
        collection_case.tenant_account_id, collection_case.invoice_draft_id
    )
    if case_invoice is None or case_invoice.issued_invoice_id != issued.issued_invoice_id:
        return CreditNoteApplicationRejectionReasonCode.INVOICE_MISMATCH
    return None


def _rejected(
    reason_code: CreditNoteApplicationRejectionReasonCode,
) -> CreditNoteApplicationResult:
    """Build a rejected result without writing an application or reducing money."""
    return CreditNoteApplicationResult(
        credit_note_application_outcome_code=CreditNoteApplicationOutcomeCode.REJECTED,
        credit_note_application_contract_version=CREDIT_NOTE_APPLICATION_CONTRACT_VERSION,
        credit_note_application_id=None,
        issued_credit_note_id=None,
        collection_case_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        applied_amount=None,
        remaining_outstanding_amount=None,
        credit_note_application_status=None,
        collection_case_status=None,
        applied_at=None,
        source_payload_hash=None,
        issued_credit_note_source_payload_hash=None,
        issued_credit_note_contract_version=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredCreditNoteApplication,
    collection_case: StoredCollectionCase,
    tenant_reference: str,
    outcome: CreditNoteApplicationOutcomeCode,
) -> CreditNoteApplicationResult:
    """Project a persisted application and the current case into the result."""
    remaining = collection_case.outstanding_amount
    if remaining == 0:
        remaining = Decimal("0")
    return CreditNoteApplicationResult(
        credit_note_application_outcome_code=outcome,
        credit_note_application_contract_version=stored.credit_note_application_contract_version,
        credit_note_application_id=stored.credit_note_application_id,
        issued_credit_note_id=stored.issued_credit_note_id,
        collection_case_id=stored.collection_case_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        applied_amount=stored.applied_amount,
        remaining_outstanding_amount=remaining,
        credit_note_application_status=stored.credit_note_application_status,
        collection_case_status=collection_case.collection_case_status,
        applied_at=stored.applied_at,
        source_payload_hash=stored.source_payload_hash,
        issued_credit_note_source_payload_hash=stored.issued_credit_note_source_payload_hash,
        issued_credit_note_contract_version=stored.issued_credit_note_contract_version,
        next_operator_action=(
            OPERATOR_ACTION_WAIT if remaining == 0 else OPERATOR_ACTION_COLLECT
        ),
        rejection_reason_code=None,
    )


def _format_applied_at(applied_at: datetime | None) -> str:
    """Render ``applied_at`` as a timezone-aware ISO 8601 instant."""
    if applied_at is None:
        raise ValueError("accepted credit note applications must include applied_at")
    return applied_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
