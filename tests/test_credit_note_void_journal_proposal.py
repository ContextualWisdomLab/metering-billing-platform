"""Credit-note void journal tests for unused issued-credit-note reversal."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    CreditAdjustmentService,
    CreditNoteApplicationService,
    IssuedCreditNoteService,
    IssuedCreditNoteVoidService,
    MemoryUsageLedger,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.accounting_export import parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.usage_ledger import (
    StoredCreditNoteApplication,
    generate_record_id,
)
from test_credit_note_application import issue_morning_credit_then_open_case
from test_http_app import invoke_http
from test_issued_credit_note_void import issue_known_morning_credit_note
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


PROPOSED_AT = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
VOIDED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def record_morning_credit_note_void(
    open_case: bool = True,
) -> tuple[MemoryUsageLedger, UUID, UUID, UUID, UUID | None]:
    """Persist one unused issued-credit-note void without composing its journal."""
    ledger, issued, collection = issue_known_morning_credit_note(open_case=open_case)
    voided = IssuedCreditNoteVoidService(
        ledger, clock=lambda: VOIDED_AT
    ).void_issued_credit_note(TENANT_ONE, issued.issued_credit_note_id)
    if voided.issued_credit_note_void_id is None:
        raise AssertionError("known morning path must persist an issued-credit-note void")
    collection_case_id = None if collection is None else collection.collection_case_id
    return (
        ledger,
        voided.issued_credit_note_void_id,
        issued.issued_credit_note_id,
        issued.credit_adjustment_id,
        collection_case_id,
    )


class CreditNoteVoidJournalProposalTests(unittest.TestCase):
    """Verify credit-note void exports stay balanced, exact, and proposal-only."""

    def test_known_void_reverses_credit_journal_roles(self) -> None:
        """A stored unused-note void becomes one balanced AR/revenue reverse."""
        ledger, void_id, issued_credit_note_id, credit_adjustment_id, collection_case_id = (
            record_morning_credit_note_void()
        )
        credit_journal = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_voids = len(ledger.issued_credit_note_voids)
        prior_applications = len(ledger.credit_note_applications)
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        case_status_before = ledger.collection_cases[collection_case_id].collection_case_status
        note_status_before = next(
            note.issued_credit_note_status for note in ledger.issued_credit_notes.values()
        )
        result = AccountingExportService(
            ledger, clock=lambda: PROPOSED_AT
        ).propose_credit_note_void_journal(TENANT_ONE, void_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertNotEqual(result.proposal_status, "posted")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.issued_credit_note_void_id, void_id)
        self.assertEqual(result.reversed_journal_proposal_id, credit_journal.proposal_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:issued_credit_note_void:{void_id}",),
        )
        stored_void = ledger.get_issued_credit_note_void(void_id)
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:issued_credit_note_void:{void_id}:"
                f"{stored_void.source_payload_hash}"
                f":v{stored_void.issued_credit_note_void_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "accounts_receivable")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "usage_revenue")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        payload = result.as_contract_dict()
        self.assertEqual(validate_journal_proposal(payload), ())
        self.assertNotIn("journal_proposal_outcome_code", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("chart_account_id", payload)
        self.assertNotIn("journal_entry_id", payload)
        self.assertNotIn("110100", str(payload))
        self.assertNotIn("210100", str(payload))
        self.assertNotIn("card_pan", payload)
        self.assertEqual(
            ledger.collection_cases[collection_case_id].collection_case_status,
            case_status_before,
        )
        stored_note = ledger.get_issued_credit_note(issued_credit_note_id)
        self.assertEqual(stored_note.issued_credit_note_status, note_status_before)
        self.assertEqual(stored_note.issued_credit_note_status, "issued")
        self.assertEqual(ledger.get_issued_credit_note_void(void_id).issued_credit_note_void_status, "recorded")
        self.assertEqual(len(ledger.issued_credit_note_voids), prior_voids)
        self.assertEqual(len(ledger.journal_proposals), prior_journals + 1)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.credit_note_applications), prior_applications)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox + 1)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_void_accept_does_not_compose_a_journal(self) -> None:
        """#72 void stays without a void journal until the explicit compose command."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        journal_ids_before = set(ledger.journal_proposals)
        first = AccountingExportService(ledger).propose_credit_note_void_journal(
            TENANT_ONE, void_id
        )
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(len(ledger.journal_proposals), len(journal_ids_before) + 1)
        self.assertNotIn(first.proposal_id, journal_ids_before)

    def test_second_propose_of_the_same_void_is_a_replay(self) -> None:
        """The same tenant and void reuse proposal_id and do not grow the store."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        service = AccountingExportService(ledger)
        first = service.propose_credit_note_void_journal(TENANT_ONE, void_id)
        second = service.propose_credit_note_void_journal(TENANT_ONE, void_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(
            second.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(second.reversed_journal_proposal_id, first.reversed_journal_proposal_id)
        self.assertEqual(len(ledger.journal_proposals), 2)
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_credit_and_void_journals_stay_separate_identities(self) -> None:
        """A credit-note void journal sits beside the existing credit proposal."""
        ledger, void_id, _note_id, credit_adjustment_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        voided = AccountingExportService(ledger).propose_credit_note_void_journal(
            TENANT_ONE, void_id
        )
        credit = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        self.assertEqual(voided.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(
            credit.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertNotEqual(voided.proposal_id, credit.proposal_id)
        self.assertEqual(voided.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(credit.proposal_lines[0].account_role_code, "usage_revenue")
        self.assertEqual(len(ledger.journal_proposals), 2)

    def test_taxed_void_reverses_tax_payable_on_the_same_journal(self) -> None:
        """A taxed unused credit note reverses AR, revenue, and tax payable."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, invoice_draft_id, 1)
        credit = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, Decimal("110.00"), "rating_correction"
        )
        issued = IssuedCreditNoteService(ledger).issue_credit_note(
            TENANT_ONE, credit.credit_adjustment_id
        )
        voided = IssuedCreditNoteVoidService(ledger).void_issued_credit_note(
            TENANT_ONE, issued.issued_credit_note_id
        )
        result = AccountingExportService(ledger).propose_credit_note_void_journal(
            TENANT_ONE, voided.issued_credit_note_void_id
        )
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(result.reversed_journal_proposal_id, credit.proposal_id)
        receivable, revenue, payable = result.proposal_lines
        self.assertEqual(receivable.account_role_code, "accounts_receivable")
        self.assertEqual(receivable.debit_amount, Decimal("110.00"))
        self.assertEqual(revenue.account_role_code, "usage_revenue")
        self.assertEqual(revenue.credit_amount, HUNDRED)
        self.assertEqual(payable.account_role_code, "tax_payable")
        self.assertEqual(payable.credit_amount, Decimal("10.00"))
        self.assertEqual(
            receivable.debit_amount, revenue.credit_amount + payable.credit_amount
        )
        self.assertEqual(validate_journal_proposal(result.as_contract_dict()), ())
        self.assertNotIn("journal_entry_id", result.as_contract_dict())

    def test_other_tenant_cannot_see_or_propose_the_first_void(self) -> None:
        """A tenant cannot propose or list another tenant's credit-note void journal."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        service = AccountingExportService(ledger)
        owned = service.propose_credit_note_void_journal(TENANT_ONE, void_id)
        crossed = service.propose_credit_note_void_journal(TENANT_TWO, void_id)
        self.assertEqual(owned.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 2)
        self.assertEqual(
            {row.journal_proposal_id for row in one_rows},
            {owned.proposal_id, owned.reversed_journal_proposal_id},
        )
        self.assertEqual(len(two_rows), 0)

    def test_missing_void_tenant_journal_and_applied_fail_closed(self) -> None:
        """A void journal cannot invent money without a stored unused-note void."""
        ledger, _void_id, issued_credit_note_id, _credit_id, collection_case_id = (
            record_morning_credit_note_void()
        )
        service = AccountingExportService(ledger)
        missing_void = service.propose_credit_note_void_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_void.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_void.rejection_reason_code,
            JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND,
        )
        missing_tenant = service.propose_credit_note_void_journal(
            "urn:cwl:missing_tenant", generate_record_id()
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        applied_ledger, applied_note, applied_case = _apply_morning_credit_note()
        applied = AccountingExportService(applied_ledger).propose_credit_note_void_journal(
            TENANT_ONE, generate_record_id()
        )
        self.assertEqual(
            applied.rejection_reason_code,
            JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND,
        )
        self.assertEqual(len(applied_ledger.issued_credit_note_voids), 0)
        leftover_ledger, leftover_id, leftover_note_id, leftover_credit_id, leftover_case = (
            record_morning_credit_note_void()
        )
        leftover_void = leftover_ledger.get_issued_credit_note_void(leftover_id)
        leftover_note = leftover_ledger.get_issued_credit_note(leftover_note_id)
        leftover_ledger.insert_credit_note_application(
            StoredCreditNoteApplication(
                credit_note_application_id=generate_record_id(),
                tenant_account_id=leftover_void.tenant_account_id,
                issued_credit_note_id=leftover_note_id,
                collection_case_id=leftover_case,
                invoice_draft_id=leftover_void.invoice_draft_id,
                issued_invoice_id=leftover_void.issued_invoice_id,
                credit_note_application_contract_version=1,
                issued_credit_note_contract_version=leftover_note.issued_credit_note_contract_version,
                source_payload_hash="sha256:" + "ab" * 32,
                issued_credit_note_source_payload_hash=leftover_note.source_payload_hash,
                currency_code="USD",
                applied_amount=KNOWN_MORNING_TOTAL,
                credit_note_application_status="applied",
                applied_at=VOIDED_AT,
            )
        )
        applied_after_void = AccountingExportService(
            leftover_ledger
        ).propose_credit_note_void_journal(TENANT_ONE, leftover_id)
        self.assertEqual(
            applied_after_void.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_NOTE_ALREADY_APPLIED,
        )
        orphan_ledger, orphan_id, _orphan_note, orphan_credit, _orphan_case = (
            record_morning_credit_note_void()
        )
        orphan_journal = orphan_ledger.find_journal_proposal_for_credit_adjustment(
            orphan_ledger.require_tenant(TENANT_ONE).tenant_account_id,
            orphan_credit,
        )
        del orphan_ledger.journal_proposals[orphan_journal.journal_proposal_id]
        del orphan_ledger.credit_journal_adjustment_index[
            (orphan_journal.tenant_account_id, orphan_credit)
        ]
        missing_original = AccountingExportService(orphan_ledger).propose_credit_note_void_journal(
            TENANT_ONE, orphan_id
        )
        self.assertEqual(
            missing_original.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_JOURNAL_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 1)
        del issued_credit_note_id, collection_case_id

    def test_currency_mismatch_and_invalid_amount_fail_closed(self) -> None:
        """Optional currency must match; zero, negative, and IEEE floats refuse."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        service = AccountingExportService(ledger)
        mismatch = service.propose_credit_note_void_journal(
            TENANT_ONE, void_id, currency_code="EUR"
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matched = service.propose_credit_note_void_journal(
            TENANT_ONE, void_id, currency_code="USD"
        )
        self.assertEqual(matched.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        stored = ledger.get_issued_credit_note_void(void_id)
        ledger.issued_credit_note_voids[void_id] = replace(stored, voided_amount=Decimal("0"))
        zero = AccountingExportService(ledger).propose_credit_note_void_journal(
            TENANT_ONE, void_id
        )
        self.assertEqual(zero.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        leftover_ledger, leftover_id, leftover_note_id, _leftover_credit, _leftover_case = (
            record_morning_credit_note_void()
        )
        leftover_stored = leftover_ledger.get_issued_credit_note_void(leftover_id)
        leftover_ledger.issued_credit_note_voids[leftover_id] = replace(
            leftover_stored, voided_amount=Decimal("0")
        )
        rejected_zero = AccountingExportService(leftover_ledger).propose_credit_note_void_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_zero.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        leftover_ledger.issued_credit_note_voids[leftover_id] = replace(
            leftover_stored, voided_amount=Decimal("-1")
        )
        rejected_negative = AccountingExportService(leftover_ledger).propose_credit_note_void_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_negative.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        leftover_ledger.issued_credit_note_voids[leftover_id] = replace(
            leftover_stored, voided_amount=0.003705  # type: ignore[arg-type]
        )
        floated = AccountingExportService(leftover_ledger).propose_credit_note_void_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        leftover_ledger.issued_credit_note_voids[leftover_id] = leftover_stored
        issued = leftover_ledger.get_issued_credit_note(leftover_note_id)
        leftover_ledger.issued_credit_notes[leftover_note_id] = replace(
            issued,
            tax_exclusive_amount=Decimal("1.00"),
            tax_amount=Decimal("1.00"),
            tax_inclusive_amount=Decimal("3.00"),
        )
        unbalanced = AccountingExportService(leftover_ledger).propose_credit_note_void_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            unbalanced.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        leftover_ledger.issued_credit_notes[leftover_note_id] = issued
        leftover_ledger.issued_credit_note_voids[leftover_id] = replace(
            leftover_stored, voided_amount=Decimal("1.00")
        )
        mismatched_inclusive = AccountingExportService(
            leftover_ledger
        ).propose_credit_note_void_journal(TENANT_ONE, leftover_id)
        self.assertEqual(
            mismatched_inclusive.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        leftover_ledger.issued_credit_notes[leftover_note_id] = replace(
            issued, tax_exclusive_amount=0.003705  # type: ignore[arg-type]
        )
        leftover_ledger.issued_credit_note_voids[leftover_id] = leftover_stored
        floated_issued = AccountingExportService(leftover_ledger).propose_credit_note_void_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            floated_issued.rejection_reason_code,
            JournalProposalRejectionReasonCode.VOIDED_AMOUNT_INVALID,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(len(leftover_ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.journal_proposals), 2)

    def test_http_compose_lists_void_journal_without_a_second_write(self) -> None:
        """POST compose then GET /v1/journal-proposals leaves one validated reverse."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), 1)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "EUR"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "currency_mismatch")
        self.assertEqual(len(ledger.journal_proposals), 1)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["proposal_status"], "validated")
        self.assertNotEqual(accepted_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", accepted_body)
        self.assertEqual(accepted_body["lines"][0]["account_role_code"], "accounts_receivable")
        self.assertEqual(
            accepted_body["lines"][0]["debit_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(accepted_body["lines"][1]["account_role_code"], "usage_revenue")
        self.assertEqual(
            accepted_body["lines"][1]["credit_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(validate_journal_proposal(accepted_body), ())
        proposal_id = accepted_body["proposal_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["proposal_id"], proposal_id)
        self.assertEqual(replay_body["proposal_status"], "validated")
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        void_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][0]["account_role_code"] == "accounts_receivable"
            and item["idempotency_key"].startswith(f"{TENANT_ONE}:issued_credit_note_void:")
        ]
        self.assertEqual(len(void_items), 1)
        self.assertEqual(void_items[0]["proposal_id"], proposal_id)
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{proposal_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["proposal_id"], proposal_id)
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{proposal_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        crossed_status, crossed_body = invoke_http(
            app,
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(crossed_status, 422)
        self.assertEqual(
            crossed_body["rejection_reason_code"], "issued_credit_note_void_not_found"
        )
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.journal_proposals), 2)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_resolver_and_ledger_guards_cover_identity(self) -> None:
        """Hollow tenant resolve raises; credit-note void journal rows stay append-only."""
        ledger, void_id, _note_id, _credit_id, _collection_case_id = (
            record_morning_credit_note_void()
        )
        service = AccountingExportService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.propose_credit_note_void_journal(TENANT_ONE, void_id)
        first = service.propose_credit_note_void_journal(TENANT_ONE, void_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        void_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(void_collision, void_collision.proposal_lines)
        self.assertEqual(stored.issued_credit_note_void_id, void_id)
        self.assertIsNone(stored.credit_adjustment_id)
        self.assertIsNone(stored.issued_invoice_void_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_issued_credit_note_void(
                stored.tenant_account_id,
                generate_record_id(),
            )
        )
        found = ledger.find_journal_proposal_for_issued_credit_note_void(
            stored.tenant_account_id,
            void_id,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)
        empty = AccountingExportService()
        rejected = empty.propose_credit_note_void_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency_type_status, currency_type_body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/issued-credit-note-voids/{void_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")
        missing_ledger, missing_void_id, _missing_note, _missing_credit, _missing_case = (
            record_morning_credit_note_void()
        )
        missing_ledger.issued_credit_notes.clear()
        missing = AccountingExportService(missing_ledger).propose_credit_note_void_journal(
            TENANT_ONE, missing_void_id
        )
        self.assertEqual(
            missing.rejection_reason_code,
            JournalProposalRejectionReasonCode.ISSUED_CREDIT_NOTE_VOID_NOT_FOUND,
        )


def _apply_morning_credit_note():
    """Issue and apply the known-morning credit note so no unused void exists."""
    ledger, issued, collection = issue_morning_credit_then_open_case()
    CreditNoteApplicationService(ledger).apply_credit_note(
        TENANT_ONE, issued.issued_credit_note_id, collection.collection_case_id
    )
    return ledger, issued, collection


if __name__ == "__main__":
    unittest.main()
