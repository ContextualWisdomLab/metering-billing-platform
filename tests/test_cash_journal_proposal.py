"""Realistic cash-journal tests for receipt amounts, replay, and tenant isolation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    CollectionCaseService,
    InvoiceDraftService,
    MemoryUsageLedger,
    PaymentIntentService,
    PaymentSettlementService,
    UsageIngestionService,
    UsageRatingService,
    format_exact_decimal,
)
from metering_billing.accounting_export import parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.usage_ledger import generate_record_id
from test_payment_intent import open_known_morning_case
from test_usage_ingestion import ACCOUNT_TWO, TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, MORNING_WINDOW, make_event


def record_known_morning_receipt() -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist the known morning receipt after projecting its payment intent."""
    ledger, collection_case_id = open_known_morning_case()
    projected = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
    if projected.payment_intent_id is None:
        raise AssertionError("known morning path must persist a payment intent")
    receipt = PaymentSettlementService(ledger).record_payment_receipt(
        TENANT_ONE, projected.payment_intent_id, KNOWN_MORNING_TOTAL
    )
    if receipt.payment_receipt_id is None:
        raise AssertionError("known morning path must persist a payment receipt")
    return ledger, receipt.payment_receipt_id, collection_case_id


class CashJournalProposalTests(unittest.TestCase):
    """Verify cash exports stay balanced, exact, and proposal-only."""

    def test_known_receipt_emits_balanced_cash_and_receivable_proposal(self) -> None:
        """A known receipt amount must become one balanced cash/AR proposal."""
        ledger, payment_receipt_id, collection_case_id = record_known_morning_receipt()
        outstanding_before = ledger.collection_cases[collection_case_id].outstanding_amount
        result = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.payment_receipt_id, payment_receipt_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:cash_receipt:{payment_receipt_id}",),
        )
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:cash_receipt:{payment_receipt_id}:"
                f"{result.source_payload_hash}:v{result.proposal_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "cash_receipt")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "accounts_receivable")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        self.assertEqual(validate_journal_proposal(result.as_contract_dict()), ())
        self.assertNotEqual(result.as_contract_dict()["proposal_status"], "posted")
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, outstanding_before)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_invoice_draft_propose_journal_still_works_unchanged(self) -> None:
        """The draft AR/revenue path must still emit its own proposal beside cash."""
        ledger, payment_receipt_id, collection_case_id = record_known_morning_receipt()
        invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
        cash = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        draft = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(cash.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(draft.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertNotEqual(cash.proposal_id, draft.proposal_id)
        self.assertEqual(draft.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(draft.proposal_lines[1].account_role_code, "usage_revenue")
        self.assertEqual(draft.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.journal_proposals), 2)
        self.assertEqual(validate_journal_proposal(draft.as_contract_dict()), ())

    def test_second_propose_of_the_same_receipt_is_a_replay(self) -> None:
        """The same tenant, receipt, hash, and contract version reuse proposal_id."""
        ledger, payment_receipt_id, _collection_case_id = record_known_morning_receipt()
        service = AccountingExportService(ledger)
        first = service.propose_cash_journal(TENANT_ONE, payment_receipt_id)
        second = service.propose_cash_journal(TENANT_ONE, payment_receipt_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_other_tenant_cannot_see_or_propose_the_first_receipt(self) -> None:
        """A tenant cannot propose or list another tenant's payment receipt."""
        ledger, one_receipt_id, _one_case_id = record_known_morning_receipt()
        foreign = make_event(
            event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf701",
            source_event_key="tenant_two:step_01",
            tenant_reference=TENANT_TWO,
            billing_account_reference=ACCOUNT_TWO,
            billing_principal_reference="urn:cwl:tenant_002:billing_principal:019d8002",
            credential_reference="urn:cwl:tenant_002:credential_record:019d8003",
            occurred_at="2026-08-16T10:27:42.482Z",
            measurements=[
                {
                    "meter_code": "gen_ai_output_token",
                    "quantity": "10",
                    "unit_code": "token",
                    "quality_code": "provider_reported",
                }
            ],
        )
        UsageIngestionService(ledger).ingest_usage_event(foreign)
        two_rate = UsageRatingService(ledger).rate_usage_window(TENANT_TWO, MORNING_WINDOW, 1)
        two_draft = InvoiceDraftService(ledger).draft_invoice(TENANT_TWO, two_rate.rating_run_id)
        opened = CollectionCaseService(ledger).open_collection_case(TENANT_TWO, two_draft.invoice_draft_id)
        two_intent = PaymentIntentService(ledger).project_payment_intent(
            TENANT_TWO, opened.collection_case_id
        )
        two_amount = Decimal("10") * Decimal("0.000002")
        two_receipt = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_TWO, two_intent.payment_intent_id, two_amount
        )
        service = AccountingExportService(ledger)
        one_proposal = service.propose_cash_journal(TENANT_ONE, one_receipt_id)
        two_proposal = service.propose_cash_journal(TENANT_TWO, two_receipt.payment_receipt_id)
        crossed = service.propose_cash_journal(TENANT_TWO, one_receipt_id)
        self.assertEqual(one_proposal.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(two_proposal.proposal_lines[0].debit_amount, two_amount)
        self.assertNotEqual(one_proposal.proposal_id, two_proposal.proposal_id)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].journal_proposal_id, one_proposal.proposal_id)
        self.assertEqual(len(two_rows), 1)

    def test_missing_receipt_and_tenant_fail_closed(self) -> None:
        """A cash proposal cannot invent money without a stored tenant receipt."""
        ledger, _payment_receipt_id, _collection_case_id = record_known_morning_receipt()
        service = AccountingExportService(ledger)
        missing_receipt = service.propose_cash_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_receipt.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_receipt.rejection_reason_code,
            JournalProposalRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND,
        )
        missing_tenant = service.propose_cash_journal("urn:cwl:missing_tenant", generate_record_id())
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_zero_receipt_amount_and_float_money_fail_closed(self) -> None:
        """Cash proposals reject zero amounts and IEEE binary floats."""
        ledger, payment_receipt_id, _collection_case_id = record_known_morning_receipt()
        stored = ledger.payment_receipts[payment_receipt_id]
        ledger.payment_receipts[payment_receipt_id] = replace(stored, received_amount=Decimal("0"))
        rejected = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.RECEIPT_AMOUNT_INVALID,
        )
        ledger.payment_receipts[payment_receipt_id] = replace(stored, received_amount=0.003705)  # type: ignore[arg-type]
        floated = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.RECEIPT_AMOUNT_INVALID,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(parse_proposal_amount("0.003705"), Decimal("0.003705"))
        self.assertEqual(format_exact_decimal(parse_proposal_amount(Decimal("0.003705"))), "0.003705")
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_default_service_and_rejected_cash_contract_stay_sparse(self) -> None:
        """The zero-argument service constructs a ledger and rejected cash exports omit money."""
        empty = AccountingExportService()
        self.assertIsInstance(empty.ledger, MemoryUsageLedger)
        rejected = empty.propose_cash_journal(TENANT_ONE, generate_record_id())
        payload = rejected.as_contract_dict()
        self.assertEqual(payload["journal_proposal_outcome_code"], "rejected")
        self.assertNotIn("proposal_id", payload)
        self.assertNotIn("lines", payload)
        self.assertNotIn("source_payload_hash", payload)


class CashJournalCatalogTests(unittest.TestCase):
    """Cover cash-proposal persistence edges without posting or capturing."""

    def test_cash_proposal_insert_is_immutable_and_receipt_scoped(self) -> None:
        """A second insert of the same receipt identity cannot replace history."""
        ledger, payment_receipt_id, _collection_case_id = record_known_morning_receipt()
        first = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        receipt_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(receipt_collision, receipt_collision.proposal_lines)
        self.assertEqual(stored.payment_receipt_id, payment_receipt_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_receipt(
                stored.tenant_account_id,
                payment_receipt_id,
                "sha256:" + "d" * 64,
                stored.proposal_contract_version,
            )
        )
        found = ledger.find_journal_proposal_for_receipt(
            stored.tenant_account_id,
            payment_receipt_id,
            stored.source_payload_hash,
            stored.proposal_contract_version,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)

    def test_orphan_receipt_without_a_case_fails_closed(self) -> None:
        """A cash proposal cannot proceed when the linked collection case is gone."""
        ledger, payment_receipt_id, collection_case_id = record_known_morning_receipt()
        del ledger.collection_cases[collection_case_id]
        rejected = AccountingExportService(ledger).propose_cash_journal(TENANT_ONE, payment_receipt_id)
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_clock_stamps_proposed_at_from_the_receipt_date(self) -> None:
        """A supplied clock stamps proposed_at; commercial dates follow the receipt."""
        received_at = datetime(2026, 8, 17, 20, 15, tzinfo=UTC)
        proposed_at = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
        ledger, collection_case_id = open_known_morning_case()
        projected = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
        receipt = PaymentSettlementService(ledger, clock=lambda: received_at).record_payment_receipt(
            TENANT_ONE, projected.payment_intent_id, KNOWN_MORNING_TOTAL
        )
        composed = next(iter(ledger.journal_proposals.values()))
        result = AccountingExportService(ledger, clock=lambda: proposed_at).propose_cash_journal(
            TENANT_ONE, receipt.payment_receipt_id
        )
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(ledger.journal_proposals[result.proposal_id].proposed_at, composed.proposed_at)
        self.assertNotEqual(composed.proposed_at, proposed_at)
        self.assertEqual(result.transaction_date, "2026-08-17")
        self.assertEqual(result.accounting_date, "2026-08-17")
        self.assertEqual(validate_journal_proposal(result.as_contract_dict()), ())


if __name__ == "__main__":
    unittest.main()
