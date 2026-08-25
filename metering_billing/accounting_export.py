"""Accounting journal proposals produced from stored drafts, receipts, credits, write-offs, leftover refunds, parked leftover, leftover applies, issued-invoice voids, and issued-credit-note voids.

The service is the buyer-facing export path:

1. Resolve the tenant.
2. Load that tenant's stored ``invoice_draft``, ``payment_receipt``,
   ``credit_adjustment``, ``collection_write_off``,
   ``unapplied_cash_refund``, ``unapplied_cash``,
   ``unapplied_cash_application``, ``issued_invoice_void``, or
   ``issued_credit_note_void``.
3. Copy the exact commercial amount into one balanced debit and credit pair.
4. Replay the same tenant, source identity, payload hash, and contract version.

The result is an ``accounting_journal_proposal`` for the Accounting
Information Platform (AIS).  It uses semantic account roles and an intended
book role.  Status stays inside the proposal lifecycle and is never posted.
This repository does not open fiscal periods or resolve statutory account IDs
(IFRS Foundation, 2024; International Organization for Standardization, 2026).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    JournalLineAmountScaleError,
    JournalProposalOutcomeCode,
    JournalProposalQueryError,
    JournalProposalRejectionReasonCode,
    require_resolved,
)
from metering_billing.exact_decimal import (
    format_exact_decimal,
    parse_exact_decimal,
    require_postable_journal_line_amounts,
)
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.unapplied_cash import UNAPPLIED_CASH_STATUS
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionWriteOff,
    StoredInvoiceDraft,
    StoredIssuedCreditNote,
    StoredIssuedCreditNoteVoid,
    StoredIssuedInvoice,
    StoredIssuedInvoiceVoid,
    StoredJournalProposal,
    StoredJournalProposalLine,
    StoredPaymentReceipt,
    StoredTaxAssessment,
    StoredUnappliedCash,
    StoredUnappliedCashApplication,
    StoredUnappliedCashRefund,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
PROPOSAL_CONTRACT_VERSION = 1
PROPOSAL_STATUS = "validated"
INTENDED_BOOK_ROLE_CODE = "primary_statutory"
RECEIVABLE_ACCOUNT_ROLE_CODE = "accounts_receivable"
REVENUE_ACCOUNT_ROLE_CODE = "usage_revenue"
CASH_RECEIPT_ACCOUNT_ROLE_CODE = "cash_receipt"
TAX_PAYABLE_ACCOUNT_ROLE_CODE = "tax_payable"
WRITE_OFF_EXPENSE_ACCOUNT_ROLE_CODE = "write_off_expense"
UNAPPLIED_CASH_ACCOUNT_ROLE_CODE = "unapplied_cash"
ALLOWED_PROPOSAL_STATUSES = frozenset({"draft", "validated", "exported", "rejected"})
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100


def parse_proposal_amount(value: Any) -> Decimal:
    """Parse a journal-proposal amount as an exact non-negative decimal.

    Binary floating-point values are rejected at this boundary so a proposal
    cannot smuggle IEEE inexact money into debit or credit totals.
    """
    if isinstance(value, Decimal):
        return parse_exact_decimal(format_exact_decimal(value))
    return parse_exact_decimal(value)


def compute_proposal_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical proposal payload."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class JournalProposalLineResult:
    """One balanced proposal line using a semantic account role."""

    line_number: int
    account_role_code: str
    debit_amount: Decimal
    credit_amount: Decimal

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the journal-proposal schema."""
        return {
            "line_number": self.line_number,
            "account_role_code": self.account_role_code,
            "debit_amount": format_exact_decimal(self.debit_amount),
            "credit_amount": format_exact_decimal(self.credit_amount),
        }


@dataclass(frozen=True)
class JournalProposalResult:
    """Buyer-facing result of proposing one tenant invoice draft to AIS."""

    journal_proposal_outcome_code: JournalProposalOutcomeCode
    proposal_contract_version: int
    proposal_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    legal_entity_reference: str | None
    intended_book_role_code: str | None
    transaction_currency: str | None
    transaction_date: str | None
    accounting_date: str | None
    source_payload_hash: str | None
    proposed_at: datetime | None
    proposal_status: str | None
    source_event_references: tuple[str, ...]
    idempotency_key: str | None
    rejection_reason_code: JournalProposalRejectionReasonCode | None
    proposal_lines: tuple[JournalProposalLineResult, ...]
    payment_receipt_id: UUID | None = None
    collection_write_off_id: UUID | None = None
    credit_adjustment_id: UUID | None = None
    unapplied_cash_refund_id: UUID | None = None
    unapplied_cash_id: UUID | None = None
    unapplied_cash_application_id: UUID | None = None
    issued_invoice_void_id: UUID | None = None
    issued_credit_note_void_id: UUID | None = None
    reversed_journal_proposal_id: UUID | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published proposal, or a sparse rejected operational result."""
        outcome = self.journal_proposal_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, JournalProposalOutcomeCode) else str(outcome)
        )
        if outcome_text == JournalProposalOutcomeCode.REJECTED:
            return {
                "proposal_contract_version": self.proposal_contract_version,
                "journal_proposal_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else "invoice_draft_not_found"
                ),
            }
        if (
            outcome_text != JournalProposalOutcomeCode.ACCEPTED
            and outcome_text != JournalProposalOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported journal proposal outcome: {outcome_text}")
        if self.proposed_at is None:
            raise ValueError("accepted journal proposals must include proposed_at")
        return {
            "proposal_id": str(self.proposal_id),
            "proposal_contract_version": self.proposal_contract_version,
            "idempotency_key": self.idempotency_key,
            "tenant_reference": self.tenant_reference,
            "legal_entity_reference": self.legal_entity_reference,
            "intended_book_role_code": self.intended_book_role_code,
            "transaction_currency": self.transaction_currency,
            "transaction_date": self.transaction_date,
            "accounting_date": self.accounting_date,
            "source_payload_hash": self.source_payload_hash,
            "proposed_at": _format_proposed_at(self.proposed_at),
            "proposal_status": self.proposal_status,
            "source_event_references": list(self.source_event_references),
            "lines": [line.as_contract_dict() for line in self.proposal_lines],
        }


@dataclass(frozen=True)
class JournalProposalPage:
    """One tenant-scoped page of published journal-proposal contracts."""

    journal_proposals: tuple[JournalProposalResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the collection envelope.  Items stay the published contract."""
        return {
            "journal_proposals": [item.as_contract_dict() for item in self.journal_proposals],
            "next_cursor": self.next_cursor,
        }


class AccountingExportService:
    """Append-only journal-proposal exporter backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _insert_accepted_proposal(
        self,
        journal_proposal: StoredJournalProposal,
        proposal_lines: tuple[StoredJournalProposalLine, ...],
        tenant_reference: str,
        project: Callable[..., JournalProposalResult] | None = None,
    ) -> JournalProposalResult:
        """Persist one validated proposal or fail closed before AIS can pull it."""
        rejected = _reject_unpostable_journal_lines(proposal_lines)
        if rejected is not None:
            return rejected
        persisted = self.ledger.insert_journal_proposal(journal_proposal, proposal_lines)
        projector = _from_stored if project is None else project
        result = projector(persisted, tenant_reference, JournalProposalOutcomeCode.ACCEPTED)
        enqueue_accepted_fact(
            self.ledger,
            tenant_reference,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            persisted.journal_proposal_id,
            result.as_contract_dict(),
            persisted.proposed_at,
        )
        return result

    def propose_journal(
        self, tenant_reference: str, invoice_draft_id: UUID
    ) -> JournalProposalResult:
        """Propose one balanced journal from a persisted invoice draft.

        A replay of the same tenant, invoice draft, source-payload hash, and
        contract version returns the stored ``proposal_id``.  Another tenant
        cannot see or propose from that draft.  AIS next pulls validated
        proposals; this service never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)

        drafted_total_amount = parse_proposal_amount(invoice_draft.drafted_total_amount)
        if drafted_total_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.DRAFT_TOTAL_INVALID)
        assessment = self.ledger.find_tax_assessment_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )

        commercial_date = invoice_draft.recorded_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = f"{tenant.tenant_reference}:invoice_draft:{invoice_draft.invoice_draft_id}"
        canonical_payload = _canonical_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            invoice_draft=invoice_draft,
            drafted_total_amount=drafted_total_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
            assessment=assessment,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        existing = self.ledger.find_journal_proposal(
            tenant.tenant_account_id,
            invoice_draft.invoice_draft_id,
            source_payload_hash,
            PROPOSAL_CONTRACT_VERSION,
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        journal_proposal_id = generate_record_id()
        stored_lines = _build_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            drafted_total_amount,
            assessment,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=invoice_draft.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:invoice_draft:{invoice_draft.invoice_draft_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=invoice_draft.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_cash_journal(
        self, tenant_reference: str, payment_receipt_id: UUID
    ) -> JournalProposalResult:
        """Propose one balanced cash/AR journal from a persisted payment receipt.

        A replay of the same tenant, payment receipt, source-payload hash, and
        contract version returns the stored ``proposal_id``.  Another tenant
        cannot see or propose from that receipt.  Collection outstanding is
        not changed.  AIS next pulls validated proposals; this service
        never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None

        payment_receipt = self.ledger.get_payment_receipt(payment_receipt_id)
        if (
            payment_receipt is None
            or payment_receipt.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(JournalProposalRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND)

        try:
            received_amount = parse_proposal_amount(payment_receipt.received_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.RECEIPT_AMOUNT_INVALID)
        if received_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.RECEIPT_AMOUNT_INVALID)

        collection_case = self.ledger.get_collection_case(payment_receipt.collection_case_id)
        if collection_case is None:
            return _rejected(JournalProposalRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND)

        commercial_date = payment_receipt.received_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:cash_receipt:{payment_receipt.payment_receipt_id}"
        )
        canonical_payload = _canonical_cash_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            payment_receipt=payment_receipt,
            received_amount=received_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        existing = self.ledger.find_journal_proposal_for_receipt(
            tenant.tenant_account_id,
            payment_receipt.payment_receipt_id,
            source_payload_hash,
            PROPOSAL_CONTRACT_VERSION,
        )
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )

        journal_proposal_id = generate_record_id()
        stored_lines = _build_cash_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            received_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:cash_receipt:{payment_receipt.payment_receipt_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=payment_receipt.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                payment_receipt_id=payment_receipt.payment_receipt_id,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_write_off_journal(
        self,
        tenant_reference: str,
        collection_write_off_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced write-off/AR journal from a stored write-off.

        A replay of the same tenant and ``collection_write_off_id`` returns
        the stored ``proposal_id``.  Another tenant cannot see or propose
        from that write-off.  Collection outstanding is not changed.
        AIS next pulls validated proposals; this service never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_write_off(
            tenant.tenant_account_id, collection_write_off_id
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        write_off = self.ledger.get_collection_write_off(collection_write_off_id)
        if write_off is None or write_off.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.COLLECTION_WRITE_OFF_NOT_FOUND)
        if currency_code is not None and currency_code != write_off.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            write_off_amount = parse_proposal_amount(write_off.write_off_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.WRITE_OFF_AMOUNT_INVALID)
        if write_off_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.WRITE_OFF_AMOUNT_INVALID)

        commercial_date = write_off.written_off_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:collection_write_off:{write_off.collection_write_off_id}"
        )
        canonical_payload = _canonical_write_off_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            write_off=write_off,
            write_off_amount=write_off_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_write_off_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            write_off_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=write_off.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:collection_write_off:"
                    f"{write_off.collection_write_off_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=write_off.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                collection_write_off_id=write_off.collection_write_off_id,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_refund_journal(
        self,
        tenant_reference: str,
        unapplied_cash_refund_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced unapplied-cash/cash journal from a stored refund.

        A replay of the same tenant and ``unapplied_cash_refund_id`` returns
        the stored ``proposal_id``.  Another tenant cannot see or propose
        from that refund.  Refund, leftover, and cash facts are not changed.
        AIS next pulls validated proposals; this service never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_refund(
            tenant.tenant_account_id, unapplied_cash_refund_id
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        refund = self.ledger.get_unapplied_cash_refund(unapplied_cash_refund_id)
        if refund is None or refund.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_REFUND_NOT_FOUND)
        collection_case = self.ledger.get_collection_case(refund.collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_REFUND_NOT_FOUND)
        if currency_code is not None and currency_code != refund.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            refund_amount = parse_proposal_amount(refund.refund_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.REFUND_AMOUNT_INVALID)
        if refund_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.REFUND_AMOUNT_INVALID)

        commercial_date = refund.refunded_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:unapplied_cash_refund:{refund.unapplied_cash_refund_id}"
        )
        canonical_payload = _canonical_refund_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            refund=refund,
            refund_amount=refund_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_refund_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            refund_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:unapplied_cash_refund:"
                    f"{refund.unapplied_cash_refund_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=refund.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                unapplied_cash_refund_id=refund.unapplied_cash_refund_id,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_unapplied_cash_journal(
        self,
        tenant_reference: str,
        unapplied_cash_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced cash/unapplied-cash journal from parked leftover.

        A replay of the same tenant and ``unapplied_cash_id`` returns the
        stored ``proposal_id``.  Another tenant cannot see or propose
        from that leftover.  Leftover, refund, and cash facts are not
        changed.  AIS next pulls validated proposals; this service never
        posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_unapplied_cash(
            tenant.tenant_account_id, unapplied_cash_id
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        leftover = self.ledger.get_unapplied_cash(unapplied_cash_id)
        if leftover is None or leftover.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND)
        collection_case = self.ledger.get_collection_case(leftover.collection_case_id)
        if (
            collection_case is None
            or collection_case.tenant_account_id != tenant.tenant_account_id
        ):
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND)
        if leftover.unapplied_cash_status != UNAPPLIED_CASH_STATUS:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_NOT_PARKED)
        if currency_code is not None and currency_code != leftover.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            leftover_amount = parse_proposal_amount(leftover.unapplied_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_AMOUNT_INVALID)
        if leftover_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_AMOUNT_INVALID)

        commercial_date = leftover.parked_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:unapplied_cash:{leftover.unapplied_cash_id}"
        )
        canonical_payload = _canonical_unapplied_cash_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            leftover=leftover,
            leftover_amount=leftover_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_unapplied_cash_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            leftover_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=collection_case.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:unapplied_cash:"
                    f"{leftover.unapplied_cash_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=leftover.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                unapplied_cash_id=leftover.unapplied_cash_id,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_unapplied_cash_application_journal(
        self,
        tenant_reference: str,
        unapplied_cash_application_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced unapplied-cash/AR journal from a leftover apply.

        A replay of the same tenant and ``unapplied_cash_application_id``
        returns the stored ``proposal_id``.  Another tenant cannot see or
        propose from that application.  Leftover, apply, refund, and cash
        facts are not changed.  AIS next pulls validated proposals; this
        service never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_unapplied_cash_application(
            tenant.tenant_account_id, unapplied_cash_application_id
        )
        if existing is not None:
            result = _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        application = self.ledger.get_unapplied_cash_application(unapplied_cash_application_id)
        if application is None or application.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.UNAPPLIED_CASH_APPLICATION_NOT_FOUND)
        if currency_code is not None and currency_code != application.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            applied_amount = parse_proposal_amount(application.applied_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.APPLIED_AMOUNT_INVALID)
        if applied_amount <= 0:
            return _rejected(JournalProposalRejectionReasonCode.APPLIED_AMOUNT_INVALID)

        commercial_date = application.applied_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:unapplied_cash_application:"
            f"{application.unapplied_cash_application_id}"
        )
        canonical_payload = _canonical_unapplied_cash_application_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            application=application,
            applied_amount=applied_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_unapplied_cash_application_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            applied_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=application.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:unapplied_cash_application:"
                    f"{application.unapplied_cash_application_id}"
                    f":{source_payload_hash}:v{PROPOSAL_CONTRACT_VERSION}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=application.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                unapplied_cash_application_id=application.unapplied_cash_application_id,
            ),
            stored_lines,
            tenant.tenant_reference,
        )

    def propose_credit_journal(
        self,
        tenant_reference: str,
        credit_adjustment_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced credit/AR journal from a stored credit.

        Credit accept already composes this write.  A replay of the same
        tenant and ``credit_adjustment_id`` returns the stored
        ``proposal_id``.  Another tenant cannot see or propose from that
        credit.  AIS next pulls validated proposals; this service never posts.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_credit_adjustment(
            tenant.tenant_account_id, credit_adjustment_id
        )
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )

        credit = self.ledger.get_credit_adjustment(credit_adjustment_id)
        if credit is None or credit.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND)
        invoice_draft = self.ledger.get_invoice_draft(credit.invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND)
        if currency_code is not None and currency_code != credit.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            credit_amount = parse_proposal_amount(credit.credit_amount)
            exclusive_amount = parse_proposal_amount(credit.tax_exclusive_amount)
            tax_amount = parse_proposal_amount(credit.tax_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID)
        if credit_amount <= 0 or exclusive_amount + tax_amount != credit_amount:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID)
        rejected = _reject_unpostable_amounts(credit_amount, exclusive_amount, tax_amount)
        if rejected is not None:
            return rejected

        # Circular: CreditAdjustmentService imports this module to enqueue
        # journal_proposal.validated.  Reuse the existing credit-journal insert
        # so this command does not invent a second proposal store.
        from metering_billing.credit_adjustment import _insert_credit_journal

        stored = _insert_credit_journal(
            self.ledger, tenant.tenant_reference, credit, invoice_draft
        )
        result = _from_stored(stored, tenant.tenant_reference, JournalProposalOutcomeCode.ACCEPTED)
        enqueue_accepted_fact(
            self.ledger,
            tenant.tenant_reference,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            stored.journal_proposal_id,
            result.as_contract_dict(),
            stored.proposed_at,
        )
        return result

    def propose_void_journal(
        self,
        tenant_reference: str,
        issued_invoice_void_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced revenue/AR reverse from a stored issued-invoice void.

        A replay of the same tenant and ``issued_invoice_void_id`` returns
        the stored ``proposal_id``.  Another tenant cannot see or propose
        from that void.  Collection and issued-invoice status are not
        changed.  AIS next pulls validated proposals; this service never
        posts and never emits statutory IDs or ``journal_entry_id``.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_issued_invoice_void(
            tenant.tenant_account_id, issued_invoice_void_id
        )
        if existing is not None:
            result = _void_result_from_stored(
                existing,
                tenant.tenant_reference,
                JournalProposalOutcomeCode.DUPLICATE_REPLAY,
                self.ledger,
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        void_row = self.ledger.get_issued_invoice_void(issued_invoice_void_id)
        if void_row is None or void_row.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.ISSUED_INVOICE_VOID_NOT_FOUND)
        issued = self.ledger.get_issued_invoice(void_row.issued_invoice_id)
        if issued is None or issued.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.ISSUED_INVOICE_VOID_NOT_FOUND)
        if currency_code is not None and currency_code != void_row.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            voided_amount = parse_proposal_amount(void_row.voided_amount)
            exclusive_amount = parse_proposal_amount(issued.tax_exclusive_amount)
            tax_amount = parse_proposal_amount(issued.tax_amount)
            inclusive_amount = parse_proposal_amount(issued.tax_inclusive_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID)
        if (
            voided_amount <= 0
            or exclusive_amount + tax_amount != inclusive_amount
            or voided_amount != inclusive_amount
        ):
            return _rejected(JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID)

        invoice_journal = self.ledger.find_journal_proposal_for_invoice_draft(
            tenant.tenant_account_id, void_row.invoice_draft_id
        )
        reversed_journal_proposal_id = (
            None if invoice_journal is None else invoice_journal.journal_proposal_id
        )
        commercial_date = void_row.voided_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:issued_invoice_void:{void_row.issued_invoice_void_id}"
        )
        canonical_payload = _canonical_void_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            void_row=void_row,
            issued=issued,
            exclusive_amount=exclusive_amount,
            tax_amount=tax_amount,
            inclusive_amount=inclusive_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
            reversed_journal_proposal_id=reversed_journal_proposal_id,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_void_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            exclusive_amount,
            tax_amount,
            inclusive_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=void_row.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:issued_invoice_void:"
                    f"{void_row.issued_invoice_void_id}"
                    f":{void_row.source_payload_hash}"
                    f":v{void_row.issued_invoice_void_contract_version}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=void_row.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                issued_invoice_void_id=void_row.issued_invoice_void_id,
            ),
            stored_lines,
            tenant.tenant_reference,
            project=lambda persisted, tenant_reference, outcome: _void_result_from_stored(
                persisted, tenant_reference, outcome, self.ledger
            ),
        )

    def propose_credit_note_void_journal(
        self,
        tenant_reference: str,
        issued_credit_note_void_id: UUID,
        currency_code: str | None = None,
    ) -> JournalProposalResult:
        """Propose one balanced AR/revenue reverse from a stored unused-note void.

        A replay of the same tenant and ``issued_credit_note_void_id``
        returns the stored ``proposal_id``.  Another tenant cannot see or
        propose from that void.  Collection, issued-credit-note, and void
        status are not changed.  AIS next pulls validated proposals; this
        service never posts and never emits statutory IDs or
        ``journal_entry_id``.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(JournalProposalRejectionReasonCode.TENANT_NOT_FOUND)
        tenant = require_resolved(tenant, "tenant")

        existing = self.ledger.find_journal_proposal_for_issued_credit_note_void(
            tenant.tenant_account_id, issued_credit_note_void_id
        )
        if existing is not None:
            result = _credit_note_void_result_from_stored(
                existing,
                tenant.tenant_reference,
                JournalProposalOutcomeCode.DUPLICATE_REPLAY,
                self.ledger,
            )
            enqueue_accepted_fact(
                self.ledger,
                tenant.tenant_reference,
                EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
                existing.journal_proposal_id,
                result.as_contract_dict(),
                existing.proposed_at,
            )
            return result

        void_row = self.ledger.get_issued_credit_note_void(issued_credit_note_void_id)
        if void_row is None or void_row.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND)
        issued = self.ledger.get_issued_credit_note(void_row.issued_credit_note_id)
        if issued is None or issued.tenant_account_id != tenant.tenant_account_id:
            return _rejected(JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND)
        if self.ledger.find_credit_note_application(
            tenant.tenant_account_id, void_row.issued_credit_note_id
        ) is not None:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED)
        if currency_code is not None and currency_code != void_row.currency_code:
            return _rejected(JournalProposalRejectionReasonCode.CURRENCY_MISMATCH)

        try:
            voided_amount = parse_proposal_amount(void_row.voided_amount)
            exclusive_amount = parse_proposal_amount(issued.tax_exclusive_amount)
            tax_amount = parse_proposal_amount(issued.tax_amount)
            inclusive_amount = parse_proposal_amount(issued.tax_inclusive_amount)
        except ExactDecimalError:
            return _rejected(JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID)
        if (
            voided_amount <= 0
            or exclusive_amount + tax_amount != inclusive_amount
            or voided_amount != inclusive_amount
        ):
            return _rejected(JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID)

        credit_journal = self.ledger.find_journal_proposal_for_credit_adjustment(
            tenant.tenant_account_id, void_row.credit_adjustment_id
        )
        if credit_journal is None:
            return _rejected(JournalProposalRejectionReasonCode.CREDIT_JOURNAL_NOT_FOUND)
        reversed_journal_proposal_id = credit_journal.journal_proposal_id
        commercial_date = void_row.voided_at.astimezone(UTC).date().isoformat()
        legal_entity_reference = f"{tenant.tenant_reference}:legal_entity:commercial"
        source_event_reference = (
            f"{tenant.tenant_reference}:issued_credit_note_void:"
            f"{void_row.issued_credit_note_void_id}"
        )
        canonical_payload = _canonical_credit_note_void_proposal_payload(
            tenant_reference=tenant.tenant_reference,
            void_row=void_row,
            issued=issued,
            exclusive_amount=exclusive_amount,
            tax_amount=tax_amount,
            inclusive_amount=inclusive_amount,
            commercial_date=commercial_date,
            legal_entity_reference=legal_entity_reference,
            source_event_reference=source_event_reference,
            reversed_journal_proposal_id=reversed_journal_proposal_id,
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        journal_proposal_id = generate_record_id()
        stored_lines = _build_credit_note_void_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            exclusive_amount,
            tax_amount,
            inclusive_amount,
        )
        return self._insert_accepted_proposal(
            StoredJournalProposal(
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=void_row.invoice_draft_id,
                proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
                idempotency_key=(
                    f"{tenant.tenant_reference}:issued_credit_note_void:"
                    f"{void_row.issued_credit_note_void_id}"
                    f":{void_row.source_payload_hash}"
                    f":v{void_row.issued_credit_note_void_contract_version}"
                ),
                legal_entity_reference=legal_entity_reference,
                intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
                transaction_currency=void_row.currency_code,
                transaction_date=commercial_date,
                accounting_date=commercial_date,
                source_payload_hash=source_payload_hash,
                proposed_at=self._clock(),
                proposal_status=PROPOSAL_STATUS,
                source_event_reference=source_event_reference,
                proposal_lines=stored_lines,
                issued_credit_note_void_id=void_row.issued_credit_note_void_id,
            ),
            stored_lines,
            tenant.tenant_reference,
            project=lambda persisted, tenant_reference, outcome: (
                _credit_note_void_result_from_stored(
                    persisted, tenant_reference, outcome, self.ledger
                )
            ),
        )

    def list_journal_proposals(
        self,
        tenant_reference: str,
        proposal_status: str | None = None,
        proposed_after: str | None = None,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> JournalProposalPage:
        """Return one tenant page of persisted proposals without mutating status.

        Order is ``proposed_at`` then ``proposal_id``.  Cash, AR, credit,
        write-off, leftover, apply, invoice-void, and credit-note-void
        proposals share ``journal_proposal`` and therefore appear in the
        same list.
        AIS owns ``posting_receipt``; this query never marks exported or posted.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise JournalProposalQueryError("tenant_not_found")
        assert tenant is not None
        status_filter = _parse_proposal_status(proposal_status)
        after_instant = _parse_proposed_after(proposed_after)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_journal_proposals(tenant.tenant_account_id),
            key=lambda proposal: (proposal.proposed_at, proposal.journal_proposal_id),
        )
        matched: list[StoredJournalProposal] = []
        for stored in stored_rows:
            if status_filter is not None and stored.proposal_status != status_filter:
                continue
            if after_instant is not None and stored.proposed_at < after_instant:
                continue
            if cursor_key is not None and (stored.proposed_at, stored.journal_proposal_id) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.proposed_at, last.journal_proposal_id)
        return JournalProposalPage(
            journal_proposals=tuple(
                _from_stored(stored, tenant.tenant_reference, JournalProposalOutcomeCode.ACCEPTED)
                for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def get_journal_proposal(
        self, tenant_reference: str, proposal_id: UUID
    ) -> JournalProposalResult:
        """Return one same-tenant published proposal, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not change ``proposal_status``.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise JournalProposalQueryError("tenant_not_found")
        assert tenant is not None
        stored = self.ledger.get_journal_proposal(proposal_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise JournalProposalQueryError("proposal_not_found")
        return _from_stored(stored, tenant.tenant_reference, JournalProposalOutcomeCode.ACCEPTED)


def _canonical_proposal_payload(
    tenant_reference: str,
    invoice_draft: StoredInvoiceDraft,
    drafted_total_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
    assessment: StoredTaxAssessment | None = None,
) -> dict[str, object]:
    """Return commercial proposal facts excluding identifiers and timestamps.

    When a tax assessment exists the hash includes the three-line AR, revenue,
    and ``tax_payable`` amounts so a taxed draft cannot collide with an
    untaxed replay of the same invoice draft.
    """
    if assessment is None:
        amount_text = format_exact_decimal(drafted_total_amount)
        lines: list[dict[str, object]] = [
            {
                "line_number": 1,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": REVENUE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ]
    else:
        lines = [
            {
                "line_number": 1,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": format_exact_decimal(assessment.tax_inclusive_amount),
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": REVENUE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": format_exact_decimal(assessment.tax_exclusive_amount),
            },
            {
                "line_number": 3,
                "account_role_code": TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": format_exact_decimal(assessment.tax_amount),
            },
        ]
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "invoice_draft_id": str(invoice_draft.invoice_draft_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": invoice_draft.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": lines,
    }


def _canonical_cash_proposal_payload(
    tenant_reference: str,
    payment_receipt: StoredPaymentReceipt,
    received_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return cash-proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(received_amount)
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "payment_receipt_id": str(payment_receipt.payment_receipt_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": payment_receipt.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": [
            {
                "line_number": 1,
                "account_role_code": CASH_RECEIPT_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ],
    }


def _canonical_write_off_proposal_payload(
    tenant_reference: str,
    write_off: StoredCollectionWriteOff,
    write_off_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return write-off-proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(write_off_amount)
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "collection_write_off_id": str(write_off.collection_write_off_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": write_off.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": [
            {
                "line_number": 1,
                "account_role_code": WRITE_OFF_EXPENSE_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ],
    }


def _canonical_refund_proposal_payload(
    tenant_reference: str,
    refund: StoredUnappliedCashRefund,
    refund_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return refund-proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(refund_amount)
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "unapplied_cash_refund_id": str(refund.unapplied_cash_refund_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": refund.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": [
            {
                "line_number": 1,
                "account_role_code": UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": CASH_RECEIPT_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ],
    }


def _canonical_unapplied_cash_application_proposal_payload(
    tenant_reference: str,
    application: StoredUnappliedCashApplication,
    applied_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return leftover-apply-proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(applied_amount)
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "unapplied_cash_application_id": str(application.unapplied_cash_application_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": application.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": [
            {
                "line_number": 1,
                "account_role_code": UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ],
    }


def _build_unapplied_cash_application_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    applied_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the unapplied-cash debit and receivable credit for one leftover apply."""
    amount = parse_proposal_amount(applied_amount)
    return (
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
            debit_amount=amount,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=amount,
        ),
    )


def _canonical_unapplied_cash_proposal_payload(
    tenant_reference: str,
    leftover: StoredUnappliedCash,
    leftover_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return leftover-proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(leftover_amount)
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "unapplied_cash_id": str(leftover.unapplied_cash_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": leftover.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": [
            {
                "line_number": 1,
                "account_role_code": CASH_RECEIPT_ACCOUNT_ROLE_CODE,
                "debit_amount": amount_text,
                "credit_amount": "0",
            },
            {
                "line_number": 2,
                "account_role_code": UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": amount_text,
            },
        ],
    }


def _build_unapplied_cash_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    leftover_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the cash-receipt debit and unapplied-cash credit for one leftover."""
    amount = parse_proposal_amount(leftover_amount)
    return (
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=CASH_RECEIPT_ACCOUNT_ROLE_CODE,
            debit_amount=amount,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=amount,
        ),
    )


def _build_refund_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    refund_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the unapplied-cash debit and cash-receipt credit for one refund."""
    amount = parse_proposal_amount(refund_amount)
    return (
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=UNAPPLIED_CASH_ACCOUNT_ROLE_CODE,
            debit_amount=amount,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=CASH_RECEIPT_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=amount,
        ),
    )


def _canonical_void_proposal_payload(
    tenant_reference: str,
    void_row: StoredIssuedInvoiceVoid,
    issued: StoredIssuedInvoice,
    exclusive_amount: Decimal,
    tax_amount: Decimal,
    inclusive_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
    reversed_journal_proposal_id: UUID | None,
) -> dict[str, object]:
    """Return void-proposal facts excluding timestamps and statutory IDs."""
    exclusive_text = format_exact_decimal(exclusive_amount)
    tax_text = format_exact_decimal(tax_amount)
    inclusive_text = format_exact_decimal(inclusive_amount)
    lines: list[dict[str, object]] = [
        {
            "line_number": 1,
            "account_role_code": REVENUE_ACCOUNT_ROLE_CODE,
            "debit_amount": exclusive_text,
            "credit_amount": "0",
        }
    ]
    if tax_amount > 0:
        lines.append(
            {
                "line_number": 2,
                "account_role_code": TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": tax_text,
                "credit_amount": "0",
            }
        )
        lines.append(
            {
                "line_number": 3,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": inclusive_text,
            }
        )
    else:
        lines.append(
            {
                "line_number": 2,
                "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": inclusive_text,
            }
        )
    payload: dict[str, object] = {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "issued_invoice_void_id": str(void_row.issued_invoice_void_id),
        "invoice_draft_id": str(void_row.invoice_draft_id),
        "issued_invoice_id": str(issued.issued_invoice_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": void_row.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": lines,
    }
    if reversed_journal_proposal_id is not None:
        payload["reversed_journal_proposal_id"] = str(reversed_journal_proposal_id)
    return payload


def _build_void_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    exclusive_amount: Decimal,
    tax_amount: Decimal,
    inclusive_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the revenue, optional tax-payable, and AR reverse of one invoice journal."""
    exclusive = parse_proposal_amount(exclusive_amount)
    tax = parse_proposal_amount(tax_amount)
    inclusive = parse_proposal_amount(inclusive_amount)
    lines = [
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=REVENUE_ACCOUNT_ROLE_CODE,
            debit_amount=exclusive,
            credit_amount=Decimal("0"),
        )
    ]
    if tax > 0:
        lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=2,
                account_role_code=TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                debit_amount=tax,
                credit_amount=Decimal("0"),
            )
        )
        lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=3,
                account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=inclusive,
            )
        )
    else:
        lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=2,
                account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=inclusive,
            )
        )
    return tuple(lines)


def _void_result_from_stored(
    stored: StoredJournalProposal,
    tenant_reference: str,
    outcome: JournalProposalOutcomeCode,
    ledger: MemoryUsageLedger,
) -> JournalProposalResult:
    """Project a void proposal and bind the original invoice journal by Billing id."""
    result = _from_stored(stored, tenant_reference, outcome)
    invoice_journal = ledger.find_journal_proposal_for_invoice_draft(
        stored.tenant_account_id, stored.invoice_draft_id
    )
    reversed_journal_proposal_id = (
        None if invoice_journal is None else invoice_journal.journal_proposal_id
    )
    return replace(result, reversed_journal_proposal_id=reversed_journal_proposal_id)


def _canonical_credit_note_void_proposal_payload(
    tenant_reference: str,
    void_row: StoredIssuedCreditNoteVoid,
    issued: StoredIssuedCreditNote,
    exclusive_amount: Decimal,
    tax_amount: Decimal,
    inclusive_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
    reversed_journal_proposal_id: UUID,
) -> dict[str, object]:
    """Return credit-note-void-proposal facts excluding timestamps and statutory IDs."""
    exclusive_text = format_exact_decimal(exclusive_amount)
    tax_text = format_exact_decimal(tax_amount)
    inclusive_text = format_exact_decimal(inclusive_amount)
    lines: list[dict[str, object]] = [
        {
            "line_number": 1,
            "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
            "debit_amount": inclusive_text,
            "credit_amount": "0",
        },
        {
            "line_number": 2,
            "account_role_code": REVENUE_ACCOUNT_ROLE_CODE,
            "debit_amount": "0",
            "credit_amount": exclusive_text,
        },
    ]
    if tax_amount > 0:
        lines.append(
            {
                "line_number": 3,
                "account_role_code": TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                "debit_amount": "0",
                "credit_amount": tax_text,
            }
        )
    return {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "issued_credit_note_void_id": str(void_row.issued_credit_note_void_id),
        "issued_credit_note_id": str(issued.issued_credit_note_id),
        "credit_adjustment_id": str(void_row.credit_adjustment_id),
        "invoice_draft_id": str(void_row.invoice_draft_id),
        "legal_entity_reference": legal_entity_reference,
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": void_row.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "reversed_journal_proposal_id": str(reversed_journal_proposal_id),
        "lines": lines,
    }


def _build_credit_note_void_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    exclusive_amount: Decimal,
    tax_amount: Decimal,
    inclusive_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the AR, revenue, and optional tax-payable reverse of one credit journal."""
    exclusive = parse_proposal_amount(exclusive_amount)
    tax = parse_proposal_amount(tax_amount)
    inclusive = parse_proposal_amount(inclusive_amount)
    lines = [
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
            debit_amount=inclusive,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=REVENUE_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=exclusive,
        ),
    ]
    if tax > 0:
        lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=3,
                account_role_code=TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=tax,
            )
        )
    return tuple(lines)


def _credit_note_void_result_from_stored(
    stored: StoredJournalProposal,
    tenant_reference: str,
    outcome: JournalProposalOutcomeCode,
    ledger: MemoryUsageLedger,
) -> JournalProposalResult:
    """Project a credit-note void proposal and bind the original credit journal."""
    result = _from_stored(stored, tenant_reference, outcome)
    void_row = ledger.get_issued_credit_note_void(stored.issued_credit_note_void_id)
    credit_journal = ledger.find_journal_proposal_for_credit_adjustment(
        stored.tenant_account_id, void_row.credit_adjustment_id
    )
    return replace(result, reversed_journal_proposal_id=credit_journal.journal_proposal_id)


def _build_write_off_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    write_off_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the write-off-expense debit and receivable credit for one write-off."""
    amount = parse_proposal_amount(write_off_amount)
    return (
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=WRITE_OFF_EXPENSE_ACCOUNT_ROLE_CODE,
            debit_amount=amount,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=amount,
        ),
    )


def _build_cash_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    received_amount: Decimal,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the cash-receipt debit and receivable credit for one receipt."""
    amount = parse_proposal_amount(received_amount)
    return (
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=CASH_RECEIPT_ACCOUNT_ROLE_CODE,
            debit_amount=amount,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=amount,
        ),
    )


def _build_proposal_lines(
    journal_proposal_id: UUID,
    tenant_account_id: UUID,
    drafted_total_amount: Decimal,
    assessment: StoredTaxAssessment | None = None,
) -> tuple[StoredJournalProposalLine, ...]:
    """Build AR/revenue lines, plus ``tax_payable`` when assessed tax is positive.

    A half-even product that rounds to zero keeps the two-line AR/revenue
    shape so every persisted line stays debit XOR credit.  The canonical
    payload still includes the tax facts so a zero-tax assessment cannot
    collide with an untaxed draft.
    """
    if assessment is None:
        amount = parse_proposal_amount(drafted_total_amount)
        return (
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=1,
                account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
                debit_amount=amount,
                credit_amount=Decimal("0"),
            ),
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=2,
                account_role_code=REVENUE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=amount,
            ),
        )
    inclusive = parse_proposal_amount(assessment.tax_inclusive_amount)
    exclusive = parse_proposal_amount(assessment.tax_exclusive_amount)
    tax_amount = parse_proposal_amount(assessment.tax_amount)
    lines = [
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=1,
            account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
            debit_amount=inclusive,
            credit_amount=Decimal("0"),
        ),
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=tenant_account_id,
            line_number=2,
            account_role_code=REVENUE_ACCOUNT_ROLE_CODE,
            debit_amount=Decimal("0"),
            credit_amount=exclusive,
        ),
    ]
    if tax_amount > 0:
        lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=tenant_account_id,
                line_number=3,
                account_role_code=TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=tax_amount,
            )
        )
    return tuple(lines)


def _reject_unpostable_amounts(*amounts: Decimal) -> JournalProposalResult | None:
    """Return a rejected proposal when any amount exceeds six fractional digits."""
    try:
        require_postable_journal_line_amounts(*amounts)
    except JournalLineAmountScaleError:
        return _rejected(JournalProposalRejectionReasonCode.JOURNAL_LINE_AMOUNT_INVALID)
    return None


def _reject_unpostable_journal_lines(
    lines: tuple[StoredJournalProposalLine, ...],
) -> JournalProposalResult | None:
    """Return a rejected proposal when any composed line exceeds AIS scale."""
    amounts: list[Decimal] = []
    for line in lines:
        amounts.append(line.debit_amount)
        amounts.append(line.credit_amount)
    return _reject_unpostable_amounts(*amounts)


def _rejected(reason_code: JournalProposalRejectionReasonCode) -> JournalProposalResult:
    """Build a rejected result without writing a proposal."""
    return JournalProposalResult(
        journal_proposal_outcome_code=JournalProposalOutcomeCode.REJECTED,
        proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
        proposal_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        legal_entity_reference=None,
        intended_book_role_code=None,
        transaction_currency=None,
        transaction_date=None,
        accounting_date=None,
        source_payload_hash=None,
        proposed_at=None,
        proposal_status=None,
        source_event_references=(),
        idempotency_key=None,
        rejection_reason_code=reason_code,
        proposal_lines=(),
    )


def _from_stored(
    stored: StoredJournalProposal,
    tenant_reference: str,
    outcome: JournalProposalOutcomeCode,
) -> JournalProposalResult:
    """Project a persisted proposal into the buyer-facing result."""
    return JournalProposalResult(
        journal_proposal_outcome_code=outcome,
        proposal_contract_version=stored.proposal_contract_version,
        proposal_id=stored.journal_proposal_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        legal_entity_reference=stored.legal_entity_reference,
        intended_book_role_code=stored.intended_book_role_code,
        transaction_currency=stored.transaction_currency,
        transaction_date=stored.transaction_date,
        accounting_date=stored.accounting_date,
        source_payload_hash=stored.source_payload_hash,
        proposed_at=stored.proposed_at,
        proposal_status=stored.proposal_status,
        source_event_references=(stored.source_event_reference,),
        idempotency_key=stored.idempotency_key,
        rejection_reason_code=None,
        proposal_lines=tuple(
            JournalProposalLineResult(
                line_number=line.line_number,
                account_role_code=line.account_role_code,
                debit_amount=line.debit_amount,
                credit_amount=line.credit_amount,
            )
            for line in stored.proposal_lines
        ),
        payment_receipt_id=stored.payment_receipt_id,
        collection_write_off_id=stored.collection_write_off_id,
        credit_adjustment_id=stored.credit_adjustment_id,
        unapplied_cash_refund_id=stored.unapplied_cash_refund_id,
        unapplied_cash_id=stored.unapplied_cash_id,
        unapplied_cash_application_id=stored.unapplied_cash_application_id,
        issued_invoice_void_id=stored.issued_invoice_void_id,
        issued_credit_note_void_id=stored.issued_credit_note_void_id,
    )


def _format_proposed_at(proposed_at: datetime) -> str:
    """Render ``proposed_at`` as a timezone-aware ISO 8601 instant."""
    return proposed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_proposal_status(proposal_status: str | None) -> str | None:
    """Accept a proposal-lifecycle status or reject an illegal filter."""
    if proposal_status is None or proposal_status == "":
        return None
    if proposal_status not in ALLOWED_PROPOSAL_STATUSES:
        raise JournalProposalQueryError("request_invalid")
    return proposal_status


def _parse_proposed_after(proposed_after: str | None) -> datetime | None:
    """Parse an inclusive ISO 8601 lower bound, or reject an unreadable instant."""
    if proposed_after is None or proposed_after == "":
        return None
    try:
        return parse_iso8601_datetime(proposed_after)
    except Exception as error:
        raise JournalProposalQueryError("request_invalid") from error


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise JournalProposalQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise JournalProposalQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise JournalProposalQueryError("request_invalid")
    return parsed


def _encode_page_cursor(proposed_at: datetime, proposal_id: UUID) -> str:
    """Encode the keyset cursor as proposed_at then proposal_id."""
    return f"{_format_proposed_at(proposed_at)}|{proposal_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        proposed_text, proposal_text = cursor.split("|", 1)
        return parse_iso8601_datetime(proposed_text), UUID(proposal_text)
    except (TypeError, ValueError) as error:
        raise JournalProposalQueryError("request_invalid") from error
