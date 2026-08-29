"""Tests for composing rated late adjustments into invoice intent."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from uuid import uuid4
from unittest import mock

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    CreditAdjustmentService,
    InvoiceDraftService,
    IssuedInvoiceService,
    LateAdjustmentApplicationService,
    LateAdjustmentInvoiceAdjustmentService,
    LateAdjustmentPresentmentService,
    LateAdjustmentRatingService,
    MemoryUsageLedger,
    IssuedInvoicePresentmentService,
    TaxAssessmentService,
    TaxRateService,
    UsageRatingService,
    create_billing_period,
    create_http_app,
    create_late_adjustment,
    validate_issued_invoice,
    validate_issued_invoice_presentment,
    validate_late_adjustment_invoice_adjustment,
)
from metering_billing.errors import ExactDecimalError
from metering_billing.exact_decimal import issued_invoice_amount_exceeds_storage_precision
from metering_billing.issued_invoice import (
    _format_signed_decimal,
    _project_draft_lines,
    _tax_amounts,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import MORNING_WINDOW, ingest_known_batch
from metering_billing.usage_ledger import (
    StoredCollectionCase,
    StoredCreditAdjustment,
    StoredJournalProposal,
    StoredJournalProposalLine,
    StoredLateAdjustmentInvoiceAdjustment,
    StoredTaxAssessment,
)
from metering_billing.late_adjustment_invoice_adjustment import (
    LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION,
)


def prepare_invoice_adjustment(amount: str = "-12.50"):
    """Build a rated late adjustment and one unissued same-currency draft."""
    ingest = ingest_known_batch()
    ledger = ingest.ledger
    rating = UsageRatingService(ledger).rate_usage_window(
        TENANT_ONE, MORNING_WINDOW, 1
    )
    draft = InvoiceDraftService(ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
    source = create_billing_period(
        TENANT_ONE,
        date(2026, 7, 1),
        date(2026, 8, 1),
        opened_by="operator:period",
        opened_at=datetime(2026, 7, 1, tzinfo=UTC),
        period_id=uuid4(),
    ).advance(
        "soft_closed",
        actor_reference="operator:period",
        authorization_reference="approval:period",
        reason="close source",
        transitioned_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    target = create_billing_period(
        TENANT_ONE,
        date(2026, 8, 1),
        date(2026, 9, 1),
        opened_by="operator:period",
        opened_at=datetime(2026, 8, 1, tzinfo=UTC),
        period_id=uuid4(),
    )
    ledger.insert_billing_period(source)
    ledger.insert_billing_period(target)
    adjustment = create_late_adjustment(
        source.period_id,
        target.period_id,
        "correction",
        amount,
        "USD",
        "provider:late-invoice-001",
        "sha256:" + "a" * 64,
        datetime(2026, 8, 2, tzinfo=UTC),
        late_adjustment_id=uuid4(),
    )
    ledger.insert_late_adjustment(TENANT_ONE, adjustment)
    LateAdjustmentApplicationService(ledger).apply_late_adjustment(
        TENANT_ONE,
        adjustment.late_adjustment_id,
        applied_by="operator:finance",
        authorization_reference="approval:apply",
    )
    LateAdjustmentRatingService(ledger).rate_late_adjustment(
        TENANT_ONE,
        adjustment.late_adjustment_id,
        rated_by="operator:finance",
        authorization_reference="approval:rate",
    )
    return ledger, adjustment, draft


def stored_candidate(ledger, adjustment, draft):
    """Build a valid direct-ledger composition candidate for boundary tests."""
    tenant = ledger.require_tenant(TENANT_ONE)
    stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
    assert stored_draft is not None
    rating = ledger.find_late_adjustment_rating(
        tenant.tenant_account_id, adjustment.late_adjustment_id
    )
    assert rating is not None
    return StoredLateAdjustmentInvoiceAdjustment(
        late_adjustment_invoice_adjustment_id=uuid4(),
        tenant_account_id=tenant.tenant_account_id,
        billing_account_id=stored_draft.invoice_draft_lines[0].billing_account_id,
        billing_account_reference=stored_draft.invoice_draft_lines[0].billing_account_reference,
        late_adjustment_rating_id=rating.late_adjustment_rating_id,
        late_adjustment_application_id=rating.late_adjustment_application_id,
        late_adjustment_id=rating.late_adjustment_id,
        invoice_draft_id=draft.invoice_draft_id,
        target_period_id=rating.target_period_id,
        adjustment_amount=rating.adjustment_amount,
        currency_code=rating.currency_code,
        recorded_by="operator:test",
        authorization_reference="approval:test",
        recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
        source_payload_hash="sha256:" + "b" * 64,
        late_adjustment_invoice_adjustment_contract_version=(
            LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION
        ),
        late_adjustment_invoice_adjustment_status="recorded",
    )


class LateAdjustmentInvoiceAdjustmentTests(unittest.TestCase):
    """Verify exact composition, replay, isolation, and issued-draft safety."""

    def test_composes_rated_delta_without_rewriting_draft(self) -> None:
        """The signed rating becomes one immutable invoice-intent fact."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(
            ledger,
            clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        )
        accepted = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        self.assertEqual(
            accepted.late_adjustment_invoice_adjustment_outcome_code, "accepted"
        )
        self.assertEqual(
            accepted.late_adjustment_invoice_adjustment_contract_version,
            LATE_ADJUSTMENT_INVOICE_ADJUSTMENT_CONTRACT_VERSION,
        )
        self.assertEqual(accepted.adjustment_amount, Decimal("-12.50"))
        self.assertEqual(validate_late_adjustment_invoice_adjustment(accepted.as_contract_dict()), ())
        self.assertEqual(len(ledger.late_adjustment_invoice_adjustments), 1)
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        self.assertIsNotNone(stored_draft)
        assert stored_draft is not None
        self.assertEqual(stored_draft.drafted_total_amount, draft.drafted_total_amount)
        self.assertEqual(len(stored_draft.invoice_draft_lines), len(draft.invoice_draft_lines))
        self.assertEqual(
            LateAdjustmentPresentmentService(ledger)
            .present_late_adjustment(TENANT_ONE, adjustment.late_adjustment_id)
            .next_operator_action,
            "issue_invoice",
        )
        replay = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:other",
            authorization_reference="approval:other",
        )
        self.assertEqual(
            replay.late_adjustment_invoice_adjustment_outcome_code,
            "duplicate_replay",
        )
        self.assertEqual(
            replay.late_adjustment_invoice_adjustment_id,
            accepted.late_adjustment_invoice_adjustment_id,
        )

    def test_issue_consumes_positive_and_negative_compositions_once(self) -> None:
        """Issuance freezes signed adjustment lines and adjusted exact totals."""
        for amount in ("-0.002", "0.002"):
            with self.subTest(amount=amount):
                ledger, adjustment, draft = prepare_invoice_adjustment(amount)
                composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    draft.invoice_draft_id,
                    recorded_by="operator:finance",
                    authorization_reference="approval:invoice-adjustment",
                )
                issued = IssuedInvoiceService(ledger).issue_invoice(
                    TENANT_ONE, draft.invoice_draft_id
                )
                expected_total = draft.drafted_total_amount + Decimal(amount)
                self.assertEqual(issued.tax_exclusive_amount, expected_total)
                self.assertEqual(issued.tax_inclusive_amount, expected_total)
                self.assertEqual(issued.issued_invoice_lines[-1].line_type, "late_adjustment")
                self.assertEqual(issued.issued_invoice_lines[-1].line_total_amount, Decimal(amount))
                self.assertEqual(
                    issued.issued_invoice_lines[-1].late_adjustment_invoice_adjustment_id,
                    composed.late_adjustment_invoice_adjustment_id,
                )
                self.assertEqual(validate_issued_invoice(issued.as_contract_dict()), ())
                presented = IssuedInvoicePresentmentService(ledger).present_issued_invoice(
                    TENANT_ONE, issued.issued_invoice_id
                )
                presented_line = presented.issued_invoice_lines[-1]
                self.assertEqual(presented_line.line_type, "late_adjustment")
                self.assertEqual(
                    presented_line.late_adjustment_invoice_adjustment_id,
                    composed.late_adjustment_invoice_adjustment_id,
                )
                self.assertEqual(
                    validate_issued_invoice_presentment(presented.as_contract_dict()), ()
                )
                replay = IssuedInvoiceService(ledger).issue_invoice(
                    TENANT_ONE, draft.invoice_draft_id
                )
                self.assertEqual(replay.issued_invoice_id, issued.issued_invoice_id)
                self.assertEqual(len(replay.issued_invoice_lines), len(issued.issued_invoice_lines))

    def test_issued_adjustment_uses_frozen_total_for_collection(self) -> None:
        """An adjusted issued snapshot can enter collection at its frozen total."""
        ledger, adjustment, draft = prepare_invoice_adjustment("-0.002")
        composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        issued = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        self.assertIsNotNone(composed.late_adjustment_invoice_adjustment_id)
        tenant = ledger.require_tenant(TENANT_ONE)
        with self.assertRaisesRegex(ValueError, "collection case does not match issued invoice"):
            ledger.insert_collection_case(
                StoredCollectionCase(
                    collection_case_id=uuid4(),
                    tenant_account_id=tenant.tenant_account_id,
                    invoice_draft_id=draft.invoice_draft_id,
                    currency_code=issued.currency_code,
                    collection_case_status="open",
                    outstanding_amount=issued.tax_inclusive_amount + Decimal("0.001"),
                    opened_at=datetime(2026, 8, 3, tzinfo=UTC),
                )
            )
        collection = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(collection.collection_case_outcome_code.value, "accepted")
        self.assertEqual(collection.outstanding_amount, issued.tax_inclusive_amount)
        replay = CollectionCaseService(ledger).open_collection_case(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(replay.collection_case_outcome_code.value, "duplicate_replay")

    def test_taxed_composition_requires_tax_reassessment_before_issue(self) -> None:
        """A stale tax snapshot cannot silently absorb a late signed delta."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", "0.10")
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, draft.invoice_draft_id, 1)
        with mock.patch.object(
            ledger,
            "list_late_adjustment_invoice_adjustments_for_draft",
            return_value=(stored_candidate(ledger, adjustment, draft),),
        ):
            rejected = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, draft.invoice_draft_id
            )
        self.assertEqual(
            rejected.rejection_reason_code.value,
            "late_adjustment_tax_reassessment_required",
        )

    def test_issue_rejects_a_negative_resulting_total(self) -> None:
        """A signed correction cannot make the commercial invoice total non-positive."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        rejected = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(rejected.rejection_reason_code.value, "request_invalid")

    def test_composition_rejects_existing_downstream_records(self) -> None:
        """A draft captured by downstream facts cannot be adjusted."""
        for downstream in ("collection", "journal", "tax", "credit"):
            with self.subTest(downstream=downstream):
                ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
                if downstream == "collection":
                    result = CollectionCaseService(ledger).open_collection_case(
                        TENANT_ONE, draft.invoice_draft_id
                    )
                    self.assertEqual(result.collection_case_outcome_code.value, "accepted")
                elif downstream == "journal":
                    result = AccountingExportService(ledger).propose_journal(
                        TENANT_ONE, draft.invoice_draft_id
                    )
                    self.assertEqual(result.journal_proposal_outcome_code.value, "accepted")
                elif downstream == "tax":
                    TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", "0.10")
                    result = TaxAssessmentService(ledger).assess_tax(
                        TENANT_ONE, draft.invoice_draft_id, 1
                    )
                    self.assertEqual(result.tax_assessment_outcome_code.value, "accepted")
                else:
                    result = CreditAdjustmentService(ledger).record_credit_adjustment(
                        TENANT_ONE,
                        draft.invoice_draft_id,
                        "0.001",
                        "rating_correction",
                    )
                    self.assertEqual(result.credit_adjustment_outcome_code.value, "accepted")
                rejected = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    draft.invoice_draft_id,
                    recorded_by="operator:finance",
                    authorization_reference="approval:invoice-adjustment",
                )
                self.assertEqual(
                    rejected.rejection_reason_code.value,
                    "invoice_draft_has_downstream_records",
                )

    def test_downstream_writes_reject_after_composition(self) -> None:
        """A composed draft cannot acquire stale downstream facts afterward."""
        for downstream in ("collection", "journal", "tax", "credit"):
            with self.subTest(downstream=downstream):
                ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
                composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    draft.invoice_draft_id,
                    recorded_by="operator:finance",
                    authorization_reference="approval:invoice-adjustment",
                )
                self.assertEqual(composed.late_adjustment_invoice_adjustment_outcome_code.value, "accepted")
                if downstream == "collection":
                    result = CollectionCaseService(ledger).open_collection_case(
                        TENANT_ONE, draft.invoice_draft_id
                    )
                    self.assertEqual(result.rejection_reason_code.value, "invoice_draft_has_late_adjustment")
                    self.assertEqual(ledger.list_collection_cases(ledger.require_tenant(TENANT_ONE).tenant_account_id), ())
                elif downstream == "journal":
                    result = AccountingExportService(ledger).propose_journal(
                        TENANT_ONE, draft.invoice_draft_id
                    )
                    self.assertEqual(result.rejection_reason_code.value, "invoice_draft_has_late_adjustment")
                    self.assertEqual(ledger.journal_proposals, {})
                elif downstream == "tax":
                    TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", "0.10")
                    result = TaxAssessmentService(ledger).assess_tax(
                        TENANT_ONE, draft.invoice_draft_id, 1
                    )
                    self.assertEqual(result.rejection_reason_code.value, "invoice_draft_has_late_adjustment")
                    self.assertEqual(ledger.tax_assessments, {})
                else:
                    result = CreditAdjustmentService(ledger).record_credit_adjustment(
                        TENANT_ONE, draft.invoice_draft_id, "0.001", "rating_correction"
                    )
                    self.assertEqual(result.rejection_reason_code.value, "invoice_draft_has_late_adjustment")
                    self.assertEqual(ledger.credit_adjustments, {})

    def test_memory_direct_downstream_inserts_reject_after_composition(self) -> None:
        """Direct memory persistence keeps the same stale-fact boundary."""
        ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
        composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        tenant = ledger.require_tenant(TENANT_ONE)
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        with self.assertRaisesRegex(ValueError, "invoice draft has late adjustment"):
            ledger.insert_collection_case(
                StoredCollectionCase(
                    collection_case_id=uuid4(),
                    tenant_account_id=tenant.tenant_account_id,
                    invoice_draft_id=draft.invoice_draft_id,
                    currency_code="USD",
                    collection_case_status="open",
                    outstanding_amount=Decimal("0.002"),
                    opened_at=datetime(2026, 8, 3, tzinfo=UTC),
                )
            )
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", "0.10")
        tax_rate = ledger.find_tax_rate_version(tenant.tenant_account_id, 1)
        assert tax_rate is not None
        with self.assertRaisesRegex(ValueError, "invoice draft has late adjustment"):
            ledger.insert_tax_assessment(
                StoredTaxAssessment(
                    tax_assessment_id=uuid4(),
                    tenant_account_id=tenant.tenant_account_id,
                    invoice_draft_id=draft.invoice_draft_id,
                    tax_rate_version_id=tax_rate.tax_rate_version_id,
                    tax_assessment_contract_version=1,
                    tax_code="vat",
                    tax_rate=Decimal("0.10"),
                    currency_code=stored_draft.currency_code,
                    tax_exclusive_amount=stored_draft.drafted_total_amount,
                    tax_amount=Decimal("0"),
                    tax_inclusive_amount=stored_draft.drafted_total_amount,
                    source_payload_hash="sha256:" + "c" * 64,
                    assessed_at=datetime(2026, 8, 3, tzinfo=UTC),
                    tax_rate_version_number=tax_rate.version_number,
                )
            )
        with self.assertRaisesRegex(ValueError, "invoice draft has late adjustment"):
            ledger.insert_credit_adjustment(
                StoredCreditAdjustment(
                    credit_adjustment_id=uuid4(),
                    tenant_account_id=tenant.tenant_account_id,
                    invoice_draft_id=draft.invoice_draft_id,
                    credit_adjustment_contract_version=1,
                    credit_reason_code="rating_correction",
                    currency_code="USD",
                    credit_amount=Decimal("0.001"),
                    tax_exclusive_amount=Decimal("0.001"),
                    tax_amount=Decimal("0"),
                    source_payload_hash="sha256:" + "d" * 64,
                    recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
                )
            )
        proposal_id = uuid4()
        proposal_lines = (
            StoredJournalProposalLine(
                journal_proposal_line_id=uuid4(),
                journal_proposal_id=proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                line_number=1,
                account_role_code="accounts_receivable",
                debit_amount=stored_draft.drafted_total_amount,
                credit_amount=Decimal("0"),
            ),
            StoredJournalProposalLine(
                journal_proposal_line_id=uuid4(),
                journal_proposal_id=proposal_id,
                tenant_account_id=tenant.tenant_account_id,
                line_number=2,
                account_role_code="revenue",
                debit_amount=Decimal("0"),
                credit_amount=stored_draft.drafted_total_amount,
            ),
        )
        with self.assertRaisesRegex(ValueError, "invoice draft has late adjustment"):
            ledger.insert_journal_proposal(
                StoredJournalProposal(
                    journal_proposal_id=proposal_id,
                    tenant_account_id=tenant.tenant_account_id,
                    invoice_draft_id=draft.invoice_draft_id,
                    proposal_contract_version=1,
                    idempotency_key="invoice-draft-direct-guard",
                    legal_entity_reference="urn:cwl:tenant_001:legal_entity:commercial",
                    intended_book_role_code="commercial",
                    transaction_currency="USD",
                    transaction_date="2026-08-03",
                    accounting_date="2026-08-03",
                    source_payload_hash="sha256:" + "e" * 64,
                    proposed_at=datetime(2026, 8, 3, tzinfo=UTC),
                    proposal_status="validated",
                    source_event_reference="urn:cwl:tenant_001:invoice-draft-direct",
                    proposal_lines=proposal_lines,
                ),
                proposal_lines,
            )
        self.assertEqual(
            composed.late_adjustment_invoice_adjustment_outcome_code.value, "accepted"
        )

    def test_downstream_services_handle_optional_and_missing_draft_locks(self) -> None:
        """Downstream services remain compatible with both lock outcomes."""
        for lock_result in (None, "missing"):
            for downstream in ("collection", "journal", "tax", "credit"):
                with self.subTest(lock_result=lock_result, downstream=downstream):
                    ledger, _, draft = prepare_invoice_adjustment("0.002")
                    if downstream == "tax":
                        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", "0.10")
                    lock = None if lock_result is None else mock.Mock(return_value=None)
                    with mock.patch.object(ledger, "lock_invoice_draft", lock):
                        if downstream == "collection":
                            result = CollectionCaseService(ledger).open_collection_case(
                                TENANT_ONE, draft.invoice_draft_id
                            )
                        elif downstream == "journal":
                            result = AccountingExportService(ledger).propose_journal(
                                TENANT_ONE, draft.invoice_draft_id
                            )
                        elif downstream == "tax":
                            result = TaxAssessmentService(ledger).assess_tax(
                                TENANT_ONE, draft.invoice_draft_id, 1
                            )
                        else:
                            result = CreditAdjustmentService(ledger).record_credit_adjustment(
                                TENANT_ONE,
                                draft.invoice_draft_id,
                                "0.001",
                                "rating_correction",
                            )
                    if lock_result is None:
                        self.assertEqual(result.rejection_reason_code, None)
                    else:
                        self.assertEqual(
                            result.rejection_reason_code.value, "invoice_draft_not_found"
                        )

    def test_composition_rejects_missing_or_ambiguous_billing_accounts(self) -> None:
        """Composition fails closed when a draft cannot identify one payer."""
        for mode, expected in (
            ("empty", "invoice_draft_billing_account_not_found"),
            ("ambiguous", "invoice_draft_billing_account_ambiguous"),
        ):
            with self.subTest(expected=expected):
                ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
                stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
                assert stored_draft is not None
                replacement_lines = ()
                if mode == "ambiguous":
                    original = stored_draft.invoice_draft_lines[0]
                    replacement_lines = (
                        original,
                        replace(
                            original,
                            billing_account_id=uuid4(),
                            billing_account_reference="urn:cwl:other:billing-account",
                        ),
                    )
                replacement = replace(stored_draft, invoice_draft_lines=replacement_lines)
                with mock.patch.object(
                    ledger, "get_invoice_draft", return_value=replacement
                ), mock.patch.object(
                    ledger, "lock_invoice_draft", return_value=replacement
                ):
                    rejected = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
                        TENANT_ONE,
                        adjustment.late_adjustment_id,
                        draft.invoice_draft_id,
                        recorded_by="operator:finance",
                        authorization_reference="approval:invoice-adjustment",
                    )
                self.assertEqual(rejected.rejection_reason_code.value, expected)

    def test_zero_resulting_total_is_rejected(self) -> None:
        """Zero-value invoices do not enter collection or issue workflows."""
        ledger, adjustment, draft = prepare_invoice_adjustment("-0.003705")
        composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        self.assertEqual(composed.late_adjustment_invoice_adjustment_outcome_code.value, "accepted")
        rejected = IssuedInvoiceService(ledger).issue_invoice(
            TENANT_ONE, draft.invoice_draft_id
        )
        self.assertEqual(rejected.rejection_reason_code.value, "request_invalid")

    def test_issue_rejects_composition_with_wrong_billing_account(self) -> None:
        """Issuance refuses a composition whose payer differs from the draft."""
        ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
        composed = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        stored = ledger.get_late_adjustment_invoice_adjustment(
            composed.late_adjustment_invoice_adjustment_id
        )
        assert stored is not None
        mismatched = replace(
            stored,
            billing_account_id=uuid4(),
            billing_account_reference="urn:cwl:other:billing-account",
        )
        with mock.patch.object(
            ledger,
            "list_late_adjustment_invoice_adjustments_for_draft",
            return_value=(mismatched,),
        ):
            rejected = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, draft.invoice_draft_id
            )
        self.assertEqual(rejected.rejection_reason_code.value, "request_invalid")

    def test_issuer_rejects_unrepresentable_totals(self) -> None:
        """Issued totals reject non-zero fractional digits beyond storage scale."""
        ledger, _, draft = prepare_invoice_adjustment("0.002")
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        with self.assertRaises(ExactDecimalError):
            _tax_amounts(
                ledger,
                replace(stored_draft, drafted_total_amount=Decimal("1.0000000000001")),
            )
        assessed = mock.Mock(
            tax_exclusive_amount=Decimal("1.0000000000001"),
            tax_amount=Decimal("0"),
            tax_inclusive_amount=Decimal("1.0000000000001"),
        )
        with mock.patch.object(
            ledger, "find_tax_assessment_for_draft", return_value=assessed
        ), self.assertRaises(ExactDecimalError):
            _tax_amounts(ledger, stored_draft)

    def test_issuer_preserves_large_exact_totals_before_storage_validation(self) -> None:
        """Large representable totals are not rounded by Decimal's default context."""
        ledger, adjustment, draft = prepare_invoice_adjustment("-0.002")
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        candidate = stored_candidate(ledger, adjustment, draft)
        adjusted_draft = replace(
            stored_draft,
            drafted_total_amount=Decimal("99999999999999999999999999.999999999999"),
        )
        adjusted_candidate = replace(candidate, adjustment_amount=Decimal("-0.000000000001"))
        with mock.patch.object(
            ledger, "get_invoice_draft", return_value=adjusted_draft
        ), mock.patch.object(
            ledger, "lock_invoice_draft", return_value=adjusted_draft
        ), mock.patch.object(
            ledger,
            "list_late_adjustment_invoice_adjustments_for_draft",
            return_value=(adjusted_candidate,),
        ):
            issued = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, draft.invoice_draft_id
            )
        self.assertEqual(issued.issued_invoice_outcome_code.value, "accepted")
        self.assertEqual(
            issued.tax_exclusive_amount,
            Decimal("99999999999999999999999999.999999999998"),
        )
        large_adjustment = Decimal("99999999999999999999999999.000000000001")
        bulk_compositions = tuple(
            replace(
                candidate,
                adjustment_amount=(
                    large_adjustment
                    if index < 100
                    else large_adjustment.copy_negate()
                ),
            )
            for index in range(200)
        )
        self.assertEqual(
            _tax_amounts(ledger, stored_draft, bulk_compositions)[0],
            stored_draft.drafted_total_amount,
        )
        projected = _project_draft_lines(
            stored_draft,
            (replace(candidate, adjustment_amount=large_adjustment),),
        )
        self.assertEqual(projected[-1].unit_price_amount, large_adjustment)

    def test_issuer_rejects_adjusted_invoice_line_limit(self) -> None:
        """A late line cannot make the issued contract exceed its 10,000-line bound."""
        ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        candidate = stored_candidate(ledger, adjustment, draft)
        original = stored_draft.invoice_draft_lines[0]
        expanded_draft = replace(
            stored_draft,
            invoice_draft_lines=tuple(
                replace(original, line_number=line_number)
                for line_number in range(1, 10001)
            ),
        )
        with mock.patch.object(
            ledger, "get_invoice_draft", return_value=expanded_draft
        ), mock.patch.object(
            ledger, "lock_invoice_draft", return_value=expanded_draft
        ), mock.patch.object(
            ledger,
            "list_late_adjustment_invoice_adjustments_for_draft",
            return_value=(candidate,),
        ):
            rejected = IssuedInvoiceService(ledger).issue_invoice(
                TENANT_ONE, draft.invoice_draft_id
            )
        self.assertEqual(rejected.rejection_reason_code.value, "request_invalid")

    def test_issue_handles_optional_and_missing_draft_locks(self) -> None:
        """The memory adapter remains compatible with both lock outcomes."""
        unlocked_ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
        LateAdjustmentInvoiceAdjustmentService(unlocked_ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        with mock.patch.object(unlocked_ledger, "lock_invoice_draft", None):
            accepted = IssuedInvoiceService(unlocked_ledger).issue_invoice(
                TENANT_ONE, draft.invoice_draft_id
            )
        self.assertEqual(accepted.issued_invoice_outcome_code.value, "accepted")

        missing_ledger, _, missing_draft = prepare_invoice_adjustment("0.002")
        with mock.patch.object(missing_ledger, "lock_invoice_draft", return_value=None):
            rejected = IssuedInvoiceService(missing_ledger).issue_invoice(
                TENANT_ONE, missing_draft.invoice_draft_id
            )
        self.assertEqual(rejected.rejection_reason_code.value, "invoice_draft_not_found")

    def test_composition_handles_optional_and_missing_draft_locks(self) -> None:
        """Composition remains fail-closed across lock adapter boundaries."""
        unlocked_ledger, adjustment, draft = prepare_invoice_adjustment("0.002")
        with mock.patch.object(unlocked_ledger, "lock_invoice_draft", None):
            accepted = LateAdjustmentInvoiceAdjustmentService(unlocked_ledger).record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:finance",
                authorization_reference="approval:invoice-adjustment",
            )
        self.assertEqual(accepted.late_adjustment_invoice_adjustment_outcome_code.value, "accepted")

        missing_ledger, missing_adjustment, missing_draft = prepare_invoice_adjustment("0.002")
        with mock.patch.object(missing_ledger, "lock_invoice_draft", return_value=None):
            rejected = LateAdjustmentInvoiceAdjustmentService(missing_ledger).record_invoice_adjustment(
                TENANT_ONE,
                missing_adjustment.late_adjustment_id,
                missing_draft.invoice_draft_id,
                recorded_by="operator:finance",
                authorization_reference="approval:invoice-adjustment",
            )
        self.assertEqual(rejected.rejection_reason_code.value, "invoice_draft_not_found")

    def test_signed_line_formatter_rejects_non_finite_amounts(self) -> None:
        """Signed line output remains finite and exact at the contract boundary."""
        self.assertTrue(issued_invoice_amount_exceeds_storage_precision(object()))
        for invalid in (object(), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ExactDecimalError):
                _format_signed_decimal(invalid)  # type: ignore[arg-type]

    def test_requires_rating_and_rejects_other_tenant_or_issued_draft(self) -> None:
        """Composition cannot bypass rating, tenant scope, or invoice immutability."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_TWO,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:finance",
                authorization_reference="approval:invoice-adjustment",
            ).rejection_reason_code,
            "late_adjustment_not_found",
        )
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, draft.invoice_draft_id)
        rejected = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        self.assertEqual(rejected.rejection_reason_code, "invoice_already_issued")

    def test_http_command_is_tenant_scoped_and_schema_valid(self) -> None:
        """The nested command accepts the same result through the HTTP adapter."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/late-adjustments/{adjustment.late_adjustment_id}/invoice-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft.invoice_draft_id),
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_late_adjustment_invoice_adjustment(body), ())
        self.assertEqual(body["next_operator_action"], "issue_invoice")
        path = f"/v1/late-adjustments/{adjustment.late_adjustment_id}/invoice-adjustments"
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft.invoice_draft_id),
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": "not-a-uuid",
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
            },
        )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        with mock.patch.object(
            LateAdjustmentInvoiceAdjustmentService,
            "record_invoice_adjustment",
            side_effect=ValueError("unexpected persistence error"),
        ):
            status, body = invoke_http(
                create_http_app(ledger),
                "POST",
                path,
                {
                    "tenant_reference": TENANT_ONE,
                    "invoice_draft_id": str(draft.invoice_draft_id),
                    "recorded_by": "operator:http",
                    "authorization_reference": "approval:http",
                },
            )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        status, body = invoke_http(create_http_app(ledger), "GET", path)
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))

    def test_service_rejects_each_precondition_and_formats_sparse_contracts(self) -> None:
        """Every rejected command remains sparse and points to one next action."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        cases = (
            ("tenant_not_found", ("urn:cwl:missing", adjustment.late_adjustment_id, draft.invoice_draft_id, "operator:x", "approval:x")),
            ("late_adjustment_not_found", (TENANT_ONE, uuid4(), draft.invoice_draft_id, "operator:x", "approval:x")),
            ("actor_reference_invalid", (TENANT_ONE, adjustment.late_adjustment_id, draft.invoice_draft_id, " ", "approval:x")),
            ("authorization_reference_invalid", (TENANT_ONE, adjustment.late_adjustment_id, draft.invoice_draft_id, "operator:x", None)),
            ("invoice_draft_not_found", (TENANT_ONE, adjustment.late_adjustment_id, "not-a-uuid", "operator:x", "approval:x")),
        )
        for reason, (tenant, late_id, draft_id, recorded_by, authorization) in cases:
            with self.subTest(reason=reason):
                result = service.record_invoice_adjustment(
                    tenant,
                    late_id,
                    draft_id,
                    recorded_by=recorded_by,
                    authorization_reference=authorization,
                )
                self.assertEqual(result.rejection_reason_code.value, reason)
                self.assertEqual(
                    result.as_contract_dict()[
                        "late_adjustment_invoice_adjustment_outcome_code"
                    ],
                    "rejected",
                )
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                "not-a-uuid",
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).rejection_reason_code.value,
            "late_adjustment_not_found",
        )

        no_rating_ledger, no_rating_adjustment, no_rating_draft = prepare_invoice_adjustment()
        no_rating_tenant = no_rating_ledger.require_tenant(TENANT_ONE)
        no_rating = no_rating_ledger.find_late_adjustment_rating(
            no_rating_tenant.tenant_account_id, no_rating_adjustment.late_adjustment_id
        )
        assert no_rating is not None
        no_rating_ledger.late_adjustment_ratings.pop(no_rating.late_adjustment_rating_id)
        no_rating_ledger.late_adjustment_rating_index.pop(
            (no_rating_tenant.tenant_account_id, no_rating_adjustment.late_adjustment_id)
        )
        self.assertEqual(
            LateAdjustmentInvoiceAdjustmentService(no_rating_ledger)
            .record_invoice_adjustment(
                TENANT_ONE,
                no_rating_adjustment.late_adjustment_id,
                no_rating_draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            )
            .rejection_reason_code.value,
            "late_adjustment_rating_not_found",
        )

        draft_missing = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            uuid4(),
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(draft_missing.rejection_reason_code.value, "invoice_draft_not_found")
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        ledger.invoice_drafts[draft.invoice_draft_id] = replace(
            stored_draft, currency_code="EUR"
        )
        currency_mismatch = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(currency_mismatch.rejection_reason_code.value, "currency_mismatch")
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                uuid4(),
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).rejection_reason_code.value,
            "invoice_draft_not_found",
        )

        malformed = replace(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ),
            rejection_reason_code=None,
        )
        self.assertEqual(
            malformed.as_contract_dict()["rejection_reason_code"], "invoice_draft_not_found"
        )

    def test_service_rejects_composition_identity_conflicts(self) -> None:
        """One rated identity cannot be attached to a second invoice draft."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).late_adjustment_invoice_adjustment_outcome_code,
            "accepted",
        )
        conflict = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            uuid4(),
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(
            conflict.rejection_reason_code.value,
            "late_adjustment_invoice_adjustment_identity_conflict",
        )

    def test_memory_insert_validates_and_replays_immutable_composition(self) -> None:
        """The in-memory authority enforces the same immutable fact boundaries."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        candidate = stored_candidate(ledger, adjustment, draft)
        self.assertIsNone(ledger.get_late_adjustment_invoice_adjustment(uuid4()))
        for bad in (
            replace(
                candidate,
                late_adjustment_invoice_adjustment_contract_version=1,
            ),
            replace(candidate, late_adjustment_invoice_adjustment_status="pending"),
            replace(candidate, currency_code="US"),
            replace(candidate, adjustment_amount=Decimal("0")),
            replace(candidate, adjustment_amount=Decimal("NaN")),
            replace(candidate, adjustment_amount=Decimal("1E+40")),
            replace(candidate, billing_account_id=None),
            replace(candidate, recorded_by=" "),
            replace(candidate, authorization_reference=" "),
            replace(candidate, source_payload_hash="invalid"),
        ):
            with self.assertRaises(ValueError):
                ledger.insert_late_adjustment_invoice_adjustment(bad)

        class NonCanonicalDecimal(Decimal):
            """Exercise the defensive canonical-string check."""

            def __format__(self, _spec: str) -> str:
                return "01"

        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_invoice_adjustment(
                replace(candidate, adjustment_amount=NonCanonicalDecimal("1"))
            )
        stored = ledger.insert_late_adjustment_invoice_adjustment(candidate)
        self.assertEqual(stored, candidate)
        replay = ledger.insert_late_adjustment_invoice_adjustment(
            replace(
                candidate,
                late_adjustment_invoice_adjustment_id=uuid4(),
                recorded_by="operator:replay",
                authorization_reference="approval:replay",
                recorded_at=datetime(2026, 8, 4, tzinfo=UTC),
            )
        )
        self.assertEqual(replay, stored)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_invoice_adjustment(
                replace(candidate, late_adjustment_invoice_adjustment_id=uuid4(), invoice_draft_id=uuid4())
            )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_invoice_adjustment(candidate)

        downstream_ledger, downstream_adjustment, downstream_draft = prepare_invoice_adjustment()
        downstream_candidate = stored_candidate(
            downstream_ledger, downstream_adjustment, downstream_draft
        )
        self.assertEqual(
            CollectionCaseService(downstream_ledger)
            .open_collection_case(TENANT_ONE, downstream_draft.invoice_draft_id)
            .collection_case_outcome_code.value,
            "accepted",
        )
        with self.assertRaisesRegex(ValueError, "invoice draft has downstream records"):
            downstream_ledger.insert_late_adjustment_invoice_adjustment(downstream_candidate)

        ambiguous_ledger, ambiguous_adjustment, ambiguous_draft = prepare_invoice_adjustment()
        ambiguous_stored_draft = ambiguous_ledger.get_invoice_draft(ambiguous_draft.invoice_draft_id)
        assert ambiguous_stored_draft is not None
        ambiguous_line = ambiguous_stored_draft.invoice_draft_lines[0]
        ambiguous_ledger.invoice_drafts[ambiguous_draft.invoice_draft_id] = replace(
            ambiguous_stored_draft,
            invoice_draft_lines=(
                ambiguous_line,
                replace(
                    ambiguous_line,
                    billing_account_id=uuid4(),
                    billing_account_reference="urn:cwl:other:billing-account",
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "invoice draft billing account is ambiguous"):
            ambiguous_ledger.insert_late_adjustment_invoice_adjustment(
                stored_candidate(ambiguous_ledger, ambiguous_adjustment, ambiguous_draft)
            )

        mismatch_ledger, mismatch_adjustment, mismatch_draft = prepare_invoice_adjustment()
        mismatch_candidate = stored_candidate(
            mismatch_ledger, mismatch_adjustment, mismatch_draft
        )
        with self.assertRaisesRegex(ValueError, "billing account does not match draft"):
            mismatch_ledger.insert_late_adjustment_invoice_adjustment(
                replace(
                    mismatch_candidate,
                    billing_account_id=uuid4(),
                    billing_account_reference="urn:cwl:other:billing-account",
                )
            )

        for variant in ("missing_rating", "missing_draft", "evidence", "issued"):
            test_ledger, test_adjustment, test_draft = prepare_invoice_adjustment()
            test_candidate = stored_candidate(test_ledger, test_adjustment, test_draft)
            if variant == "missing_rating":
                test_candidate = replace(test_candidate, late_adjustment_rating_id=uuid4())
            elif variant == "missing_draft":
                test_candidate = replace(test_candidate, invoice_draft_id=uuid4())
            elif variant == "evidence":
                test_candidate = replace(test_candidate, adjustment_amount=Decimal("-12.51"))
            else:
                IssuedInvoiceService(test_ledger).issue_invoice(
                    TENANT_ONE, test_draft.invoice_draft_id
                )
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                test_ledger.insert_late_adjustment_invoice_adjustment(test_candidate)

    def test_service_maps_insert_race_and_contract_validation_edges(self) -> None:
        """The service remains fail-closed when issuance wins between prechecks."""
        ledger, adjustment, draft = prepare_invoice_adjustment()

        class IssuanceRaceLedger:
            """Simulate an issued-invoice race after the service precheck."""

            def __init__(self, delegate: MemoryUsageLedger) -> None:
                self.delegate = delegate

            def find_issued_invoice(self, *_args):
                return None

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError("invoice draft already has an issued invoice")

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        raced = LateAdjustmentInvoiceAdjustmentService(IssuanceRaceLedger(ledger)).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(raced.rejection_reason_code.value, "invoice_already_issued")

        class PrecisionRaceLedger(IssuanceRaceLedger):
            """Simulate a storage-precision race after the service precheck."""

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError("adjustment_amount exceeds numeric(38,12) precision")

        precision_race = LateAdjustmentInvoiceAdjustmentService(
            PrecisionRaceLedger(ledger)
        ).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(
            precision_race.rejection_reason_code.value,
            "adjustment_amount_not_representable",
        )

        class IdentityConflictRaceLedger(IssuanceRaceLedger):
            """Simulate another transaction winning the rated-identity race."""

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError(
                    "late adjustment invoice adjustment identity conflicts with an existing row"
                )

        identity_conflict = LateAdjustmentInvoiceAdjustmentService(
            IdentityConflictRaceLedger(ledger)
        ).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(
            identity_conflict.rejection_reason_code.value,
            "late_adjustment_invoice_adjustment_identity_conflict",
        )

        class UnexpectedLedger(IssuanceRaceLedger):
            """Preserve unexpected persistence errors for the caller."""

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError("unexpected persistence error")

        with self.assertRaisesRegex(ValueError, "unexpected persistence error"):
            LateAdjustmentInvoiceAdjustmentService(UnexpectedLedger(ledger)).record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            )

        accepted = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        with self.assertRaises(ValueError):
            replace(accepted, source_payload_hash=None).as_contract_dict()
        self.assertTrue(validate_late_adjustment_invoice_adjustment(None))
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": "0"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": "not-a-decimal"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": Decimal("-1")}
            )
        )
