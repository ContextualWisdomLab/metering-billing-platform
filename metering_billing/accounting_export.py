"""Accounting journal proposals produced from stored drafts and payment receipts.

The service is the buyer-facing export path:

1. Resolve the tenant.
2. Load that tenant's stored ``invoice_draft`` or ``payment_receipt``.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredInvoiceDraft,
    StoredJournalProposal,
    StoredJournalProposalLine,
    StoredPaymentReceipt,
    generate_record_id,
)


Clock = Callable[[], datetime]
PROPOSAL_CONTRACT_VERSION = 1
PROPOSAL_STATUS = "validated"
INTENDED_BOOK_ROLE_CODE = "primary_statutory"
RECEIVABLE_ACCOUNT_ROLE_CODE = "accounts_receivable"
REVENUE_ACCOUNT_ROLE_CODE = "usage_revenue"
CASH_RECEIPT_ACCOUNT_ROLE_CODE = "cash_receipt"


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


class AccountingExportService:
    """Append-only journal-proposal exporter backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def propose_journal(
        self, tenant_reference: str, invoice_draft_id: UUID
    ) -> JournalProposalResult:
        """Propose one balanced journal from a persisted invoice draft.

        A replay of the same tenant, invoice draft, source-payload hash, and
        contract version returns the stored ``proposal_id``.  Another tenant
        cannot see or propose from that draft.  The operator next hands the
        proposal to AIS; this service never posts.
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
        )
        source_payload_hash = compute_proposal_payload_hash(canonical_payload)
        existing = self.ledger.find_journal_proposal(
            tenant.tenant_account_id,
            invoice_draft.invoice_draft_id,
            source_payload_hash,
            PROPOSAL_CONTRACT_VERSION,
        )
        if existing is not None:
            return _from_stored(
                existing, tenant.tenant_reference, JournalProposalOutcomeCode.DUPLICATE_REPLAY
            )

        journal_proposal_id = generate_record_id()
        stored_lines = _build_proposal_lines(
            journal_proposal_id,
            tenant.tenant_account_id,
            drafted_total_amount,
        )
        stored = self.ledger.insert_journal_proposal(
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
        )
        return _from_stored(stored, tenant.tenant_reference, JournalProposalOutcomeCode.ACCEPTED)

    def propose_cash_journal(
        self, tenant_reference: str, payment_receipt_id: UUID
    ) -> JournalProposalResult:
        """Propose one balanced cash/AR journal from a persisted payment receipt.

        A replay of the same tenant, payment receipt, source-payload hash, and
        contract version returns the stored ``proposal_id``.  Another tenant
        cannot see or propose from that receipt.  Collection outstanding is
        not changed.  The operator next hands the proposal to AIS; this
        service never posts.
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
        stored = self.ledger.insert_journal_proposal(
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
        )
        return _from_stored(stored, tenant.tenant_reference, JournalProposalOutcomeCode.ACCEPTED)


def _canonical_proposal_payload(
    tenant_reference: str,
    invoice_draft: StoredInvoiceDraft,
    drafted_total_amount: Decimal,
    commercial_date: str,
    legal_entity_reference: str,
    source_event_reference: str,
) -> dict[str, object]:
    """Return commercial proposal facts excluding identifiers and timestamps."""
    amount_text = format_exact_decimal(drafted_total_amount)
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
        "lines": [
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
        ],
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
) -> tuple[StoredJournalProposalLine, ...]:
    """Build the receivable debit and usage-revenue credit for one draft total."""
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
    )


def _format_proposed_at(proposed_at: datetime) -> str:
    """Render ``proposed_at`` as a timezone-aware ISO 8601 instant."""
    return proposed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
