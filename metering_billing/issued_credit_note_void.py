"""Immutable commercial void of one unused issued credit note.

The service is the buyer-facing void path:

1. Resolve the tenant and same-tenant ``issued_credit_note``.
2. Refuse when that note has already been applied to a collection case.
3. Persist one append-only ``issued_credit_note_void`` per issued note.
4. Leave collection remaining unchanged.  The note was unused.

Replay of the same tenant and ``issued_credit_note_id`` returns the stored
void.  First successful void enqueues one ``credit_note.voided`` outbox
event; replay is ``duplicate_replay`` with crash-heal enqueue.  The path
does not emit a journal, refund, write-off, settlement, or AIS call.  The
issued snapshot stays ``issued``; history is the void row.
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
    IssuedCreditNoteVoidOutcomeCode,
    IssuedCreditNoteVoidRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import format_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredIssuedCreditNote,
    StoredIssuedCreditNoteVoid,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_CREDIT_NOTE_VOIDED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
ISSUED_CREDIT_NOTE_VOID_CONTRACT_VERSION = 1
ISSUED_CREDIT_NOTE_VOID_STATUS = "recorded"
OPERATOR_ACTION_WAIT = "wait"


def compute_issued_credit_note_void_payload_hash(payload: Mapping[str, Any]) -> str:
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
class IssuedCreditNoteVoidResult:
    """Buyer-facing result of voiding one issued credit note."""

    issued_credit_note_void_outcome_code: IssuedCreditNoteVoidOutcomeCode | str
    issued_credit_note_void_contract_version: int
    issued_credit_note_void_id: UUID | None
    issued_credit_note_id: UUID | None
    credit_adjustment_id: UUID | None
    invoice_draft_id: UUID | None
    issued_invoice_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    voided_amount: Decimal | None
    issued_credit_note_void_status: str | None
    voided_at: datetime | None
    source_payload_hash: str | None
    next_operator_action: str
    rejection_reason_code: IssuedCreditNoteVoidRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published void, or a sparse rejected result."""
        outcome = self.issued_credit_note_void_outcome_code
        outcome_text = (
            outcome.value
            if isinstance(outcome, IssuedCreditNoteVoidOutcomeCode)
            else str(outcome)
        )
        if outcome_text == IssuedCreditNoteVoidOutcomeCode.REJECTED:
            return {
                "issued_credit_note_void_contract_version": (
                    self.issued_credit_note_void_contract_version
                ),
                "issued_credit_note_void_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else IssuedCreditNoteVoidRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != IssuedCreditNoteVoidOutcomeCode.ACCEPTED
            and outcome_text != IssuedCreditNoteVoidOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported issued-credit-note void outcome: {outcome_text}")
        payload: dict[str, object] = {
            "issued_credit_note_void_contract_version": (
                self.issued_credit_note_void_contract_version
            ),
            "issued_credit_note_void_outcome_code": outcome_text,
            "issued_credit_note_void_id": str(self.issued_credit_note_void_id),
            "tenant_reference": self.tenant_reference,
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "voided_amount": format_exact_decimal(self.voided_amount),
            "issued_credit_note_void_status": self.issued_credit_note_void_status,
            "voided_at": _format_voided_at(self.voided_at),
            "source_payload_hash": self.source_payload_hash,
            "next_operator_action": self.next_operator_action,
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload

    def as_webhook_event_data(self) -> dict[str, object]:
        """Return the thin ``credit_note.voided`` facts for the #24 envelope.

        The payload is a reference plus hash, not a remaining snapshot or
        collection status.  PII, PAN, secrets, and statutory identifiers
        are omitted.
        """
        if (
            self.issued_credit_note_void_id is None
            or self.issued_credit_note_id is None
            or self.credit_adjustment_id is None
            or self.invoice_draft_id is None
        ):
            raise ValueError("rejected issued-credit-note void has no webhook event data")
        if self.voided_at is None:
            raise ValueError("accepted issued-credit-note voids must include voided_at")
        payload: dict[str, object] = {
            "issued_credit_note_void_id": str(self.issued_credit_note_void_id),
            "issued_credit_note_id": str(self.issued_credit_note_id),
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "invoice_draft_id": str(self.invoice_draft_id),
            "source_payload_hash": self.source_payload_hash,
            "issued_credit_note_void_contract_version": (
                self.issued_credit_note_void_contract_version
            ),
            "currency_code": self.currency_code,
            "voided_amount": format_exact_decimal(self.voided_amount),
            "issued_credit_note_void_status": self.issued_credit_note_void_status,
            "voided_at": _format_voided_at(self.voided_at),
        }
        if self.issued_invoice_id is not None:
            payload["issued_invoice_id"] = str(self.issued_invoice_id)
        return payload


class IssuedCreditNoteVoidService:
    """Append-only writer of one commercial issued-credit-note void."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def void_issued_credit_note(
        self,
        tenant_reference: str,
        issued_credit_note_id: UUID,
        currency_code: str | None = None,
    ) -> IssuedCreditNoteVoidResult:
        """Void one same-tenant issued credit note when it is unused.

        Replay of the same tenant and ``issued_credit_note_id`` returns the
        stored ``issued_credit_note_void_id``.  Another tenant cannot see
        or void that note.  The issued snapshot stays ``issued``.  Collection
        remaining is unchanged because the note was never applied.  First
        successful void enqueues one ``credit_note.voided`` outbox event.
        Replay of that void does not enqueue a second row.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(IssuedCreditNoteVoidRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")
        existing = self.ledger.find_issued_credit_note_void(
            tenant.tenant_account_id, issued_credit_note_id
        )
        if existing is not None:
            result = _from_stored(
                existing,
                tenant.tenant_reference,
                IssuedCreditNoteVoidOutcomeCode.DUPLICATE_REPLAY,
            )
            _enqueue_credit_note_voided(self.ledger, tenant.tenant_reference, result)
            return result
        issued = self.ledger.get_issued_credit_note(issued_credit_note_id)
        if issued is None or issued.tenant_account_id != tenant.tenant_account_id:
            return _rejected(
                IssuedCreditNoteVoidRejectionReasonCode.ISSUED_CREDIT_NOTE_NOT_FOUND
            )
        if currency_code is not None and currency_code != issued.currency_code:
            return _rejected(IssuedCreditNoteVoidRejectionReasonCode.CURRENCY_MISMATCH)
        if (
            self.ledger.find_credit_note_application(
                tenant.tenant_account_id, issued.issued_credit_note_id
            )
            is not None
        ):
            return _rejected(
                IssuedCreditNoteVoidRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED
            )
        source_payload_hash = compute_issued_credit_note_void_payload_hash(
            _canonical_void_snapshot(issued)
        )
        stored = self.ledger.insert_issued_credit_note_void(
            StoredIssuedCreditNoteVoid(
                issued_credit_note_void_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                issued_credit_note_id=issued.issued_credit_note_id,
                credit_adjustment_id=issued.credit_adjustment_id,
                invoice_draft_id=issued.invoice_draft_id,
                issued_invoice_id=issued.issued_invoice_id,
                issued_credit_note_void_contract_version=(
                    ISSUED_CREDIT_NOTE_VOID_CONTRACT_VERSION
                ),
                source_payload_hash=source_payload_hash,
                currency_code=issued.currency_code,
                voided_amount=issued.tax_inclusive_amount,
                issued_credit_note_void_status=ISSUED_CREDIT_NOTE_VOID_STATUS,
                voided_at=self._clock(),
            )
        )
        result = _from_stored(
            stored,
            tenant.tenant_reference,
            IssuedCreditNoteVoidOutcomeCode.ACCEPTED,
        )
        _enqueue_credit_note_voided(self.ledger, tenant.tenant_reference, result)
        return result


def _enqueue_credit_note_voided(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    result: IssuedCreditNoteVoidResult,
) -> None:
    """Append one ``credit_note.voided`` outbox row for a stored void.

    Replay of the same tenant, event type, ``issued_credit_note_void_id``,
    and payload hash returns the stored row.  A crash after insert and
    before enqueue is healed by the next void replay.
    """
    if result.issued_credit_note_void_id is None or result.voided_at is None:
        raise ValueError(
            "accepted issued-credit-note voids must include identity and voided_at"
        )
    enqueue_accepted_fact(
        ledger,
        tenant_reference,
        EVENT_TYPE_CREDIT_NOTE_VOIDED,
        result.issued_credit_note_void_id,
        result.as_webhook_event_data(),
        result.voided_at,
    )


def _canonical_void_snapshot(issued: StoredIssuedCreditNote) -> dict[str, object]:
    """Return note, credit, draft, currency, inclusive amount, and version."""
    return {
        "issued_credit_note_id": str(issued.issued_credit_note_id),
        "credit_adjustment_id": str(issued.credit_adjustment_id),
        "invoice_draft_id": str(issued.invoice_draft_id),
        "currency_code": issued.currency_code,
        "voided_amount": format_exact_decimal(issued.tax_inclusive_amount),
        "issued_credit_note_void_contract_version": (
            ISSUED_CREDIT_NOTE_VOID_CONTRACT_VERSION
        ),
    }


def _rejected(
    reason_code: IssuedCreditNoteVoidRejectionReasonCode | None,
) -> IssuedCreditNoteVoidResult:
    """Build a rejected result without writing a void or changing outstanding."""
    return IssuedCreditNoteVoidResult(
        issued_credit_note_void_outcome_code=IssuedCreditNoteVoidOutcomeCode.REJECTED,
        issued_credit_note_void_contract_version=ISSUED_CREDIT_NOTE_VOID_CONTRACT_VERSION,
        issued_credit_note_void_id=None,
        issued_credit_note_id=None,
        credit_adjustment_id=None,
        invoice_draft_id=None,
        issued_invoice_id=None,
        tenant_reference=None,
        currency_code=None,
        voided_amount=None,
        issued_credit_note_void_status=None,
        voided_at=None,
        source_payload_hash=None,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredIssuedCreditNoteVoid,
    tenant_reference: str,
    outcome: IssuedCreditNoteVoidOutcomeCode,
) -> IssuedCreditNoteVoidResult:
    """Project a persisted void into the result."""
    return IssuedCreditNoteVoidResult(
        issued_credit_note_void_outcome_code=outcome,
        issued_credit_note_void_contract_version=(
            stored.issued_credit_note_void_contract_version
        ),
        issued_credit_note_void_id=stored.issued_credit_note_void_id,
        issued_credit_note_id=stored.issued_credit_note_id,
        credit_adjustment_id=stored.credit_adjustment_id,
        invoice_draft_id=stored.invoice_draft_id,
        issued_invoice_id=stored.issued_invoice_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        voided_amount=stored.voided_amount,
        issued_credit_note_void_status=stored.issued_credit_note_void_status,
        voided_at=stored.voided_at,
        source_payload_hash=stored.source_payload_hash,
        next_operator_action=OPERATOR_ACTION_WAIT,
        rejection_reason_code=None,
    )


def _format_voided_at(voided_at: datetime | None) -> str:
    """Render ``voided_at`` as a timezone-aware ISO 8601 instant."""
    if voided_at is None:
        raise ValueError("accepted issued-credit-note voids must include voided_at")
    return voided_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
