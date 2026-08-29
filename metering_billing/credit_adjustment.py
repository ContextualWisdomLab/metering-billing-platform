"""Commercial credit adjustments recorded against stored invoice drafts.

The service is the buyer-facing credit path:

1. Resolve the tenant and that tenant's stored ``invoice_draft``.
2. Accept an exact positive ``credit_amount`` that does not exceed remaining
   adjustable consideration on the draft.
3. If a collection case exists, reduce outstanding by the same inclusive
   amount.
4. When a tax assessment exists, split the inclusive credit proportionally
   and emit a three-line unwind.  Untaxed credits stay two-line.
5. Replay the same tenant, draft, amount, reason, payload hash, and version.

The credit is a commercial consideration adjustment, not a posted reversal and
not an ISO 20022 credit-note settlement message (IFRS Foundation, 2024;
International Organization for Standardization, 2026).  AIS pulls the
validated proposal; this service never posts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID

from metering_billing.accounting_export import (
    AccountingExportService,
    INTENDED_BOOK_ROLE_CODE,
    PROPOSAL_CONTRACT_VERSION,
    PROPOSAL_STATUS,
    RECEIVABLE_ACCOUNT_ROLE_CODE,
    REVENUE_ACCOUNT_ROLE_CODE,
    TAX_PAYABLE_ACCOUNT_ROLE_CODE,
    compute_proposal_payload_hash,
    parse_proposal_amount,
)
from metering_billing.errors import (
    CreditAdjustmentOutcomeCode,
    CreditAdjustmentQueryError,
    CreditAdjustmentRejectionReasonCode,
    ExactDecimalError,
    JournalLineAmountScaleError,
)
from metering_billing.exact_decimal import (
    format_exact_decimal,
    require_postable_journal_line_amounts,
)
from metering_billing.invoice_draft import parse_invoice_amount
from metering_billing.tax_assessment import CurrencyExponentError, round_tax_amount
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredCollectionCase,
    StoredCreditAdjustment,
    StoredInvoiceDraft,
    StoredJournalProposal,
    StoredJournalProposalLine,
    generate_record_id,
)
from metering_billing.webhook_outbox import (
    EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED,
    EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
    enqueue_accepted_fact,
)


Clock = Callable[[], datetime]
CREDIT_ADJUSTMENT_CONTRACT_VERSION = 1
CREDIT_ADJUSTMENT_STATUS = "recorded"
ALLOWED_CREDIT_REASON_CODES = frozenset({"rating_correction", "goodwill", "billing_error"})
CREDIT_JOURNAL_ACTION = "Record the credit; AIS pulls the validated three-line unwind."


class CreditSplitError(ValueError):
    """Raised when an inclusive credit cannot be split into exclusive and tax."""


def split_inclusive_credit(
    credit_amount: Any,
    tax_amount: Any,
    tax_inclusive_amount: Any,
    currency_code: str,
) -> tuple[Decimal, Decimal]:
    """Split an inclusive credit into exclusive and tax amounts.

    ``credit_tax_amount`` is ``round_half_even(credit_amount * tax_amount /
    tax_inclusive_amount)`` to the currency minor units.  Exclusive is the
    remainder so the parts always sum to ``credit_amount``.  A full credit of
    the assessed inclusive amount therefore reconstructs the original
    ``tax_amount`` exactly (IFRS Foundation, 2024; International Organization
    for Standardization, 2015).
    """
    parsed_credit = parse_credit_amount(credit_amount)
    try:
        parsed_tax = parse_invoice_amount(tax_amount)
        parsed_inclusive = parse_invoice_amount(tax_inclusive_amount)
    except ExactDecimalError as error:
        raise CreditSplitError("tax split amounts must be exact decimals") from error
    if parsed_inclusive <= 0 or parsed_tax < 0 or parsed_tax > parsed_inclusive:
        raise CreditSplitError("tax split inputs are not a valid inclusive assessment")
    try:
        credit_tax_amount = round_tax_amount(
            parsed_credit * parsed_tax / parsed_inclusive, currency_code
        )
    except CurrencyExponentError as error:
        raise CreditSplitError("currency exponent is unknown") from error
    credit_tax_exclusive = parsed_credit - credit_tax_amount
    if credit_tax_exclusive < 0 or credit_tax_exclusive + credit_tax_amount != parsed_credit:
        raise CreditSplitError("tax split does not sum to the credit")
    return credit_tax_exclusive, credit_tax_amount


def parse_credit_amount(value: Any) -> Decimal:
    """Parse a credit amount as an exact non-negative decimal.

    Binary floating-point values are rejected at this boundary so a credit
    cannot smuggle IEEE inexact money into remaining adjustable consideration.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ExactDecimalError("credit amount must be an exact decimal")
    if isinstance(value, Decimal):
        return parse_invoice_amount(value)
    return parse_invoice_amount(value)


def compute_credit_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical credit identity."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CreditAdjustmentResult:
    """Buyer-facing result of recording one commercial credit adjustment."""

    credit_adjustment_outcome_code: CreditAdjustmentOutcomeCode
    credit_adjustment_contract_version: int
    credit_adjustment_id: UUID | None
    invoice_draft_id: UUID | None
    tenant_reference: str | None
    currency_code: str | None
    credit_amount: Decimal | None
    credit_reason_code: str | None
    remaining_adjustable_amount: Decimal | None
    remaining_outstanding_amount: Decimal | None
    collection_case_id: UUID | None
    collection_case_status: str | None
    proposal_id: UUID | None
    proposal_status: str | None
    source_payload_hash: str | None
    idempotency_key: str | None
    recorded_at: datetime | None
    next_operator_action: str
    rejection_reason_code: CreditAdjustmentRejectionReasonCode | None
    tax_exclusive_amount: Decimal | None = None
    tax_amount: Decimal | None = None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published credit, or a sparse rejected operational result."""
        outcome = self.credit_adjustment_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, CreditAdjustmentOutcomeCode) else str(outcome)
        )
        if outcome_text == CreditAdjustmentOutcomeCode.REJECTED:
            return {
                "credit_adjustment_contract_version": self.credit_adjustment_contract_version,
                "credit_adjustment_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != CreditAdjustmentOutcomeCode.ACCEPTED
            and outcome_text != CreditAdjustmentOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported credit adjustment outcome: {outcome_text}")
        if (
            self.credit_adjustment_id is None
            or self.recorded_at is None
            or self.credit_amount is None
            or self.remaining_adjustable_amount is None
        ):
            raise ValueError("accepted credits must include identity, amount, and remaining")
        payload: dict[str, object] = {
            "credit_adjustment_contract_version": self.credit_adjustment_contract_version,
            "credit_adjustment_outcome_code": outcome_text,
            "credit_adjustment_id": str(self.credit_adjustment_id),
            "tenant_reference": self.tenant_reference,
            "invoice_draft_id": str(self.invoice_draft_id),
            "currency_code": self.currency_code,
            "credit_adjustment_status": CREDIT_ADJUSTMENT_STATUS,
            "credit_reason_code": self.credit_reason_code,
            "credit_amount": format_exact_decimal(self.credit_amount),
            "tax_exclusive_amount": format_exact_decimal(
                self.tax_exclusive_amount
                if self.tax_exclusive_amount is not None
                else self.credit_amount
            ),
            "tax_amount": format_exact_decimal(
                self.tax_amount if self.tax_amount is not None else Decimal("0")
            ),
            "remaining_adjustable_amount": format_exact_decimal(self.remaining_adjustable_amount),
            "proposal_id": str(self.proposal_id),
            "proposal_status": self.proposal_status,
            "source_payload_hash": self.source_payload_hash,
            "idempotency_key": self.idempotency_key,
            "recorded_at": _format_recorded_at(self.recorded_at),
            "next_operator_action": self.next_operator_action,
        }
        if self.collection_case_id is not None:
            payload["collection_case_id"] = str(self.collection_case_id)
            payload["collection_case_status"] = self.collection_case_status
            payload["remaining_outstanding_amount"] = format_exact_decimal(
                self.remaining_outstanding_amount
            )
        return payload


class CreditAdjustmentService:
    """Append-only commercial credit recorder backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def record_credit_adjustment(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        credit_amount: object,
        credit_reason_code: str,
    ) -> CreditAdjustmentResult:
        """Record one credit inside the repository transaction boundary."""
        transaction = getattr(self.ledger, "transaction", None)
        if transaction is None:
            return self._record_credit_adjustment(
                tenant_reference, invoice_draft_id, credit_amount, credit_reason_code
            )
        with transaction():
            return self._record_credit_adjustment(
                tenant_reference, invoice_draft_id, credit_amount, credit_reason_code
            )

    def _record_credit_adjustment(
        self,
        tenant_reference: str,
        invoice_draft_id: UUID,
        credit_amount: object,
        credit_reason_code: str,
    ) -> CreditAdjustmentResult:
        """Record one commercial credit against a persisted invoice draft.

        A replay of the same tenant, draft, amount, reason, source-payload
        hash, and contract version returns the stored ``credit_adjustment_id``
        and ``proposal_id``.  Another tenant cannot see or credit that draft.
        AIS next pulls the validated proposal; this service never posts.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(CreditAdjustmentRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(CreditAdjustmentRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if not isinstance(credit_reason_code, str) or credit_reason_code not in ALLOWED_CREDIT_REASON_CODES:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_REASON_INVALID)
        invoice_draft = self.ledger.get_invoice_draft(invoice_draft_id)
        if invoice_draft is None or invoice_draft.tenant_account_id != tenant.tenant_account_id:
            return _rejected(CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        locked_draft = getattr(self.ledger, "lock_invoice_draft", None)
        if locked_draft is not None:
            invoice_draft = locked_draft(tenant.tenant_account_id, invoice_draft.invoice_draft_id)
            if invoice_draft is None:
                return _rejected(CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
        try:
            parsed_amount = parse_credit_amount(credit_amount)
        except ExactDecimalError:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID)
        if parsed_amount <= 0:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID)
        try:
            tax_exclusive_amount, tax_amount, taxed = _credit_split_for_draft(
                self.ledger, tenant.tenant_account_id, invoice_draft, parsed_amount
            )
        except CreditSplitError:
            return _rejected(CreditAdjustmentRejectionReasonCode.TAX_SPLIT_INVALID)
        try:
            require_postable_journal_line_amounts(
                parsed_amount, tax_exclusive_amount, tax_amount
            )
        except JournalLineAmountScaleError:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_AMOUNT_INVALID)

        source_payload_hash = compute_credit_payload_hash(
            _canonical_credit_snapshot(
                invoice_draft,
                parsed_amount,
                credit_reason_code,
                tax_exclusive_amount if taxed else None,
                tax_amount if taxed else None,
            )
        )
        existing = self.ledger.find_credit_adjustment(
            tenant.tenant_account_id,
            invoice_draft.invoice_draft_id,
            source_payload_hash,
            CREDIT_ADJUSTMENT_CONTRACT_VERSION,
        )
        if existing is not None:
            proposal = self.ledger.find_journal_proposal_for_credit(
                tenant.tenant_account_id,
                existing.credit_adjustment_id,
                _credit_journal_hash(tenant.tenant_reference, existing),
                PROPOSAL_CONTRACT_VERSION,
            )
            if proposal is None:
                return _rejected(CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
            collection_case = self.ledger.find_collection_case(
                tenant.tenant_account_id, invoice_draft.invoice_draft_id
            )
            return _from_stored(
                existing,
                invoice_draft,
                proposal,
                collection_case,
                tenant.tenant_reference,
                CreditAdjustmentOutcomeCode.DUPLICATE_REPLAY,
                _remaining_adjustable(self.ledger, tenant.tenant_account_id, invoice_draft),
            )
        if self.ledger.list_late_adjustment_invoice_adjustments_for_draft(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        ):
            return _rejected(CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_HAS_LATE_ADJUSTMENT)

        remaining_adjustable = _remaining_adjustable(
            self.ledger, tenant.tenant_account_id, invoice_draft
        )
        if parsed_amount > remaining_adjustable:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_EXCEEDS_REMAINING)
        collection_case = self.ledger.find_collection_case(
            tenant.tenant_account_id, invoice_draft.invoice_draft_id
        )
        if collection_case is not None and parsed_amount > collection_case.outstanding_amount:
            return _rejected(CreditAdjustmentRejectionReasonCode.CREDIT_EXCEEDS_OUTSTANDING)

        candidate_credit_adjustment_id = generate_record_id()
        stored = self.ledger.insert_credit_adjustment(
            StoredCreditAdjustment(
                credit_adjustment_id=candidate_credit_adjustment_id,
                tenant_account_id=tenant.tenant_account_id,
                invoice_draft_id=invoice_draft.invoice_draft_id,
                credit_adjustment_contract_version=CREDIT_ADJUSTMENT_CONTRACT_VERSION,
                credit_reason_code=credit_reason_code,
                currency_code=invoice_draft.currency_code,
                credit_amount=parsed_amount,
                tax_exclusive_amount=tax_exclusive_amount,
                tax_amount=tax_amount,
                source_payload_hash=source_payload_hash,
                recorded_at=self._clock(),
            )
        )
        if stored.credit_adjustment_id != candidate_credit_adjustment_id:
            proposal = self.ledger.find_journal_proposal_for_credit(
                tenant.tenant_account_id,
                stored.credit_adjustment_id,
                _credit_journal_hash(tenant.tenant_reference, stored),
                PROPOSAL_CONTRACT_VERSION,
            )
            if proposal is None:
                return _rejected(CreditAdjustmentRejectionReasonCode.INVOICE_DRAFT_NOT_FOUND)
            return _from_stored(
                stored,
                invoice_draft,
                proposal,
                collection_case,
                tenant.tenant_reference,
                CreditAdjustmentOutcomeCode.DUPLICATE_REPLAY,
                _remaining_adjustable(self.ledger, tenant.tenant_account_id, invoice_draft),
            )
        proposal = _insert_credit_journal(self.ledger, tenant.tenant_reference, stored, invoice_draft)
        if collection_case is not None:
            collection_case = self.ledger.apply_collection_settlement(
                collection_case.collection_case_id, parsed_amount
            )
        result = _from_stored(
            stored,
            invoice_draft,
            proposal,
            collection_case,
            tenant.tenant_reference,
            CreditAdjustmentOutcomeCode.ACCEPTED,
            remaining_adjustable - parsed_amount,
        )
        enqueue_accepted_fact(
            self.ledger,
            tenant.tenant_reference,
            EVENT_TYPE_CREDIT_ADJUSTMENT_RECORDED,
            stored.credit_adjustment_id,
            result.as_contract_dict(),
            stored.recorded_at,
        )
        assert result.proposal_id is not None
        journal = AccountingExportService(self.ledger).get_journal_proposal(
            tenant.tenant_reference, result.proposal_id
        )
        assert journal.proposed_at is not None
        enqueue_accepted_fact(
            self.ledger,
            tenant.tenant_reference,
            EVENT_TYPE_JOURNAL_PROPOSAL_VALIDATED,
            result.proposal_id,
            journal.as_contract_dict(),
            journal.proposed_at,
        )
        return result

    def get_credit_adjustment(
        self, tenant_reference: str, credit_adjustment_id: UUID
    ) -> CreditAdjustmentResult:
        """Return one same-tenant stored credit, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not call AIS and does not flip ``proposal_status``.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            raise CreditAdjustmentQueryError("tenant_not_found")
        if not isinstance(credit_adjustment_id, UUID):
            raise CreditAdjustmentQueryError("credit_adjustment_not_found")
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise CreditAdjustmentQueryError("tenant_not_found")
        assert tenant is not None
        stored = self.ledger.get_credit_adjustment(credit_adjustment_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise CreditAdjustmentQueryError("credit_adjustment_not_found")
        invoice_draft = self.ledger.get_invoice_draft(stored.invoice_draft_id)
        if invoice_draft is None:
            raise CreditAdjustmentQueryError("credit_adjustment_not_found")
        proposal = self.ledger.find_journal_proposal_for_credit(
            tenant.tenant_account_id,
            stored.credit_adjustment_id,
            _credit_journal_hash(tenant.tenant_reference, stored),
            PROPOSAL_CONTRACT_VERSION,
        )
        if proposal is None:
            raise CreditAdjustmentQueryError("credit_adjustment_not_found")
        collection_case = self.ledger.find_collection_case(
            tenant.tenant_account_id, stored.invoice_draft_id
        )
        return _from_stored(
            stored,
            invoice_draft,
            proposal,
            collection_case,
            tenant.tenant_reference,
            CreditAdjustmentOutcomeCode.ACCEPTED,
            _remaining_adjustable(self.ledger, tenant.tenant_account_id, invoice_draft),
        )


def _canonical_credit_snapshot(
    invoice_draft: StoredInvoiceDraft,
    credit_amount: Decimal,
    credit_reason_code: str,
    tax_exclusive_amount: Decimal | None = None,
    tax_amount: Decimal | None = None,
) -> dict[str, object]:
    """Return draft, amount, reason, currency, and taxed-split facts."""
    payload: dict[str, object] = {
        "invoice_draft_id": str(invoice_draft.invoice_draft_id),
        "credit_amount": format_exact_decimal(credit_amount),
        "credit_reason_code": credit_reason_code,
        "currency_code": invoice_draft.currency_code,
        "credit_adjustment_contract_version": CREDIT_ADJUSTMENT_CONTRACT_VERSION,
    }
    if tax_exclusive_amount is not None and tax_amount is not None:
        payload["tax_exclusive_amount"] = format_exact_decimal(tax_exclusive_amount)
        payload["tax_amount"] = format_exact_decimal(tax_amount)
    return payload


def _credit_split_for_draft(
    ledger: MemoryUsageLedger,
    tenant_account_id: UUID,
    invoice_draft: StoredInvoiceDraft,
    credit_amount: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """Return exclusive, tax, and whether an assessment supplied the split."""
    assessment = ledger.find_tax_assessment_for_draft(
        tenant_account_id, invoice_draft.invoice_draft_id
    )
    if assessment is None:
        return credit_amount, Decimal("0"), False
    try:
        exclusive = parse_invoice_amount(assessment.tax_exclusive_amount)
        tax = parse_invoice_amount(assessment.tax_amount)
        inclusive = parse_invoice_amount(assessment.tax_inclusive_amount)
    except ExactDecimalError as error:
        raise CreditSplitError("assessment amounts are not exact decimals") from error
    if exclusive + tax != inclusive:
        raise CreditSplitError("assessment amounts do not sum")
    credit_exclusive, credit_tax = split_inclusive_credit(
        credit_amount, tax, inclusive, invoice_draft.currency_code
    )
    return credit_exclusive, credit_tax, True


def _remaining_adjustable(
    ledger: MemoryUsageLedger, tenant_account_id: UUID, invoice_draft: StoredInvoiceDraft
) -> Decimal:
    """Return inclusive (or draft) total minus already-recorded credits.

    When a tax assessment exists the adjustable base is
    ``tax_inclusive_amount``.  The paired credit journal unwinds
    ``tax_payable`` when the assessed tax split is positive.
    """
    assessment = ledger.find_tax_assessment_for_draft(
        tenant_account_id, invoice_draft.invoice_draft_id
    )
    drafted_total = parse_invoice_amount(
        assessment.tax_inclusive_amount
        if assessment is not None
        else invoice_draft.drafted_total_amount
    )
    prior_total = sum(
        (
            credit.credit_amount
            for credit in ledger.list_credit_adjustments(tenant_account_id)
            if credit.invoice_draft_id == invoice_draft.invoice_draft_id
        ),
        Decimal("0"),
    )
    return drafted_total - prior_total


def _credit_journal_hash(tenant_reference: str, credit: StoredCreditAdjustment) -> str:
    """Return the journal-proposal payload hash for one stored credit."""
    commercial_date = credit.recorded_at.astimezone(UTC).date().isoformat()
    source_event_reference = (
        f"{tenant_reference}:credit_adjustment:{credit.credit_adjustment_id}"
    )
    payload = {
        "proposal_contract_version": PROPOSAL_CONTRACT_VERSION,
        "tenant_reference": tenant_reference,
        "credit_adjustment_id": str(credit.credit_adjustment_id),
        "legal_entity_reference": f"{tenant_reference}:legal_entity:commercial",
        "intended_book_role_code": INTENDED_BOOK_ROLE_CODE,
        "transaction_currency": credit.currency_code,
        "transaction_date": commercial_date,
        "accounting_date": commercial_date,
        "proposal_status": PROPOSAL_STATUS,
        "source_event_references": [source_event_reference],
        "lines": _credit_journal_line_payloads(credit),
    }
    return compute_proposal_payload_hash(payload)


def _credit_journal_line_payloads(credit: StoredCreditAdjustment) -> list[dict[str, object]]:
    """Return canonical journal lines, including tax unwind when tax is positive."""
    exclusive_text = format_exact_decimal(credit.tax_exclusive_amount)
    tax_text = format_exact_decimal(credit.tax_amount)
    inclusive_text = format_exact_decimal(credit.credit_amount)
    lines: list[dict[str, object]] = [
        {
            "line_number": 1,
            "account_role_code": REVENUE_ACCOUNT_ROLE_CODE,
            "debit_amount": exclusive_text,
            "credit_amount": "0",
        }
    ]
    if credit.tax_amount > 0:
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
        return lines
    lines.append(
        {
            "line_number": 2,
            "account_role_code": RECEIVABLE_ACCOUNT_ROLE_CODE,
            "debit_amount": "0",
            "credit_amount": inclusive_text,
        }
    )
    return lines


def _insert_credit_journal(
    ledger: MemoryUsageLedger,
    tenant_reference: str,
    credit: StoredCreditAdjustment,
    invoice_draft: StoredInvoiceDraft,
) -> StoredJournalProposal:
    """Persist the balanced credit journal, with tax-payable unwind when taxed."""
    exclusive = parse_proposal_amount(credit.tax_exclusive_amount)
    tax_amount = parse_proposal_amount(credit.tax_amount)
    inclusive = parse_proposal_amount(credit.credit_amount)
    require_postable_journal_line_amounts(exclusive, tax_amount, inclusive)
    journal_proposal_id = generate_record_id()
    source_payload_hash = _credit_journal_hash(tenant_reference, credit)
    commercial_date = credit.recorded_at.astimezone(UTC).date().isoformat()
    source_event_reference = (
        f"{tenant_reference}:credit_adjustment:{credit.credit_adjustment_id}"
    )
    stored_lines = [
        StoredJournalProposalLine(
            journal_proposal_line_id=generate_record_id(),
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=credit.tenant_account_id,
            line_number=1,
            account_role_code=REVENUE_ACCOUNT_ROLE_CODE,
            debit_amount=exclusive,
            credit_amount=Decimal("0"),
        )
    ]
    if tax_amount > 0:
        stored_lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=credit.tenant_account_id,
                line_number=2,
                account_role_code=TAX_PAYABLE_ACCOUNT_ROLE_CODE,
                debit_amount=tax_amount,
                credit_amount=Decimal("0"),
            )
        )
        stored_lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=credit.tenant_account_id,
                line_number=3,
                account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=inclusive,
            )
        )
    else:
        stored_lines.append(
            StoredJournalProposalLine(
                journal_proposal_line_id=generate_record_id(),
                journal_proposal_id=journal_proposal_id,
                tenant_account_id=credit.tenant_account_id,
                line_number=2,
                account_role_code=RECEIVABLE_ACCOUNT_ROLE_CODE,
                debit_amount=Decimal("0"),
                credit_amount=inclusive,
            )
        )
    lines = tuple(stored_lines)
    return ledger.insert_journal_proposal(
        StoredJournalProposal(
            journal_proposal_id=journal_proposal_id,
            tenant_account_id=credit.tenant_account_id,
            invoice_draft_id=invoice_draft.invoice_draft_id,
            proposal_contract_version=PROPOSAL_CONTRACT_VERSION,
            idempotency_key=(
                f"{tenant_reference}:credit_adjustment:{credit.credit_adjustment_id}:"
                f"{credit.source_payload_hash}:v{credit.credit_adjustment_contract_version}"
            ),
            legal_entity_reference=f"{tenant_reference}:legal_entity:commercial",
            intended_book_role_code=INTENDED_BOOK_ROLE_CODE,
            transaction_currency=credit.currency_code,
            transaction_date=commercial_date,
            accounting_date=commercial_date,
            source_payload_hash=source_payload_hash,
            proposed_at=credit.recorded_at,
            proposal_status=PROPOSAL_STATUS,
            source_event_reference=source_event_reference,
            proposal_lines=lines,
            credit_adjustment_id=credit.credit_adjustment_id,
        ),
        lines,
    )


def _rejected(reason: CreditAdjustmentRejectionReasonCode) -> CreditAdjustmentResult:
    """Return a sparse rejected credit result."""
    return CreditAdjustmentResult(
        credit_adjustment_outcome_code=CreditAdjustmentOutcomeCode.REJECTED,
        credit_adjustment_contract_version=CREDIT_ADJUSTMENT_CONTRACT_VERSION,
        credit_adjustment_id=None,
        invoice_draft_id=None,
        tenant_reference=None,
        currency_code=None,
        credit_amount=None,
        credit_reason_code=None,
        remaining_adjustable_amount=None,
        remaining_outstanding_amount=None,
        collection_case_id=None,
        collection_case_status=None,
        proposal_id=None,
        proposal_status=None,
        source_payload_hash=None,
        idempotency_key=None,
        recorded_at=None,
        next_operator_action=CREDIT_JOURNAL_ACTION,
        rejection_reason_code=reason,
    )


def _from_stored(
    stored: StoredCreditAdjustment,
    invoice_draft: StoredInvoiceDraft,
    proposal: StoredJournalProposal,
    collection_case: StoredCollectionCase | None,
    tenant_reference: str,
    outcome: CreditAdjustmentOutcomeCode,
    remaining_adjustable_amount: Decimal,
) -> CreditAdjustmentResult:
    """Project a persisted credit and its proposal into the buyer-facing result."""
    return CreditAdjustmentResult(
        credit_adjustment_outcome_code=outcome,
        credit_adjustment_contract_version=stored.credit_adjustment_contract_version,
        credit_adjustment_id=stored.credit_adjustment_id,
        invoice_draft_id=stored.invoice_draft_id,
        tenant_reference=tenant_reference,
        currency_code=stored.currency_code,
        credit_amount=stored.credit_amount,
        tax_exclusive_amount=stored.tax_exclusive_amount,
        tax_amount=stored.tax_amount,
        credit_reason_code=stored.credit_reason_code,
        remaining_adjustable_amount=remaining_adjustable_amount,
        remaining_outstanding_amount=(
            collection_case.outstanding_amount if collection_case is not None else None
        ),
        collection_case_id=(
            collection_case.collection_case_id if collection_case is not None else None
        ),
        collection_case_status=(
            collection_case.collection_case_status if collection_case is not None else None
        ),
        proposal_id=proposal.journal_proposal_id,
        proposal_status=proposal.proposal_status,
        source_payload_hash=stored.source_payload_hash,
        idempotency_key=(
            f"{tenant_reference}:credit_adjustment:{stored.credit_adjustment_id}:"
            f"{stored.source_payload_hash}:v{stored.credit_adjustment_contract_version}"
        ),
        recorded_at=stored.recorded_at,
        next_operator_action=CREDIT_JOURNAL_ACTION,
        rejection_reason_code=None,
    )


def _format_recorded_at(recorded_at: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for the credit instant."""
    return recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
