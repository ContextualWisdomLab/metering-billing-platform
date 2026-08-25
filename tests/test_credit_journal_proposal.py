"""Credit journal tests for stored adjustments, replay, and AIS pull."""

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
    MemoryUsageLedger,
    TaxAssessmentService,
    TaxRateService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.accounting_export import parse_proposal_amount
from metering_billing.contracts import validate_journal_proposal
from metering_billing.credit_adjustment import CREDIT_ADJUSTMENT_CONTRACT_VERSION
from metering_billing.errors import (
    ExactDecimalError,
    JournalProposalOutcomeCode,
    JournalProposalRejectionReasonCode,
)
from metering_billing.usage_ledger import StoredCreditAdjustment, generate_record_id
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_tax_assessment import HUNDRED, STANDARD_TAX_RATE, insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL, seed_rated_ledger


PROPOSED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PARTIAL_CREDIT = Decimal("0.001")


def _invoice_draft_id_for_case(ledger: MemoryUsageLedger, collection_case_id: UUID) -> UUID:
    """Return the draft identity stored on one collection case."""
    return ledger.collection_cases[collection_case_id].invoice_draft_id


def record_morning_credit_without_journal(
    credit_amount: Decimal | None = None,
) -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist one credit adjustment without composing its journal."""
    ledger, collection_case_id = open_known_morning_case()
    invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
    invoice_draft = ledger.get_invoice_draft(invoice_draft_id)
    if invoice_draft is None:
        raise AssertionError("known morning path must persist an invoice draft")
    amount = KNOWN_MORNING_TOTAL if credit_amount is None else credit_amount
    tenant = ledger.require_tenant(TENANT_ONE)
    stored = ledger.insert_credit_adjustment(
        StoredCreditAdjustment(
            credit_adjustment_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            invoice_draft_id=invoice_draft.invoice_draft_id,
            credit_adjustment_contract_version=CREDIT_ADJUSTMENT_CONTRACT_VERSION,
            credit_reason_code="rating_correction",
            currency_code=invoice_draft.currency_code,
            credit_amount=amount,
            tax_exclusive_amount=amount,
            tax_amount=Decimal("0"),
            source_payload_hash="sha256:" + "b" * 64,
            recorded_at=PROPOSED_AT,
        )
    )
    return ledger, stored.credit_adjustment_id, collection_case_id


def record_morning_credit() -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist one accepted credit and its existing journal."""
    ledger, collection_case_id = open_known_morning_case()
    invoice_draft_id = _invoice_draft_id_for_case(ledger, collection_case_id)
    accepted = CreditAdjustmentService(ledger).record_credit_adjustment(
        TENANT_ONE, invoice_draft_id, KNOWN_MORNING_TOTAL, "rating_correction"
    )
    if accepted.credit_adjustment_id is None:
        raise AssertionError("known morning path must persist a credit adjustment")
    return ledger, accepted.credit_adjustment_id, collection_case_id


class CreditJournalProposalTests(unittest.TestCase):
    """Verify credit exports stay balanced, exact, and proposal-only."""

    def test_known_credit_emits_balanced_revenue_and_receivable_proposal(self) -> None:
        """A stored credit becomes one balanced usage-revenue/AR proposal."""
        ledger, credit_adjustment_id, collection_case_id = record_morning_credit_without_journal()
        prior_receipts = len(ledger.payment_receipts)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_write_offs = len(ledger.collection_write_offs)
        outstanding_before = ledger.collection_cases[collection_case_id].outstanding_amount
        result = AccountingExportService(
            ledger, clock=lambda: PROPOSED_AT
        ).propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertNotEqual(result.proposal_status, "posted")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.credit_adjustment_id, credit_adjustment_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:credit_adjustment:{credit_adjustment_id}",),
        )
        stored_credit = ledger.get_credit_adjustment(credit_adjustment_id)
        if stored_credit is None:
            raise AssertionError("heal path must persist the credit adjustment")
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:credit_adjustment:{credit_adjustment_id}:"
                f"{stored_credit.source_payload_hash}:v{stored_credit.credit_adjustment_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "usage_revenue")
        self.assertEqual(debit.debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "accounts_receivable")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        payload = result.as_contract_dict()
        self.assertEqual(validate_journal_proposal(payload), ())
        self.assertNotIn("journal_proposal_outcome_code", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("chart_account_id", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(ledger.collection_cases[collection_case_id].outstanding_amount, outstanding_before)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)

    def test_credit_accept_already_composes_a_journal(self) -> None:
        """#17 credit accept already writes the journal; explicit compose is replay."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit()
        self.assertEqual(len(ledger.journal_proposals), 1)
        prior_ids = set(ledger.journal_proposals)
        replay = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        self.assertEqual(
            replay.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertIn(replay.proposal_id, prior_ids)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(replay.proposal_lines[0].account_role_code, "usage_revenue")
        self.assertEqual(replay.proposal_lines[1].account_role_code, "accounts_receivable")

    def test_second_propose_of_the_same_credit_is_a_replay(self) -> None:
        """The same tenant and credit reuse proposal_id and do not grow the store."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal()
        service = AccountingExportService(ledger)
        first = service.propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        second = service.propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_invoice_and_credit_journals_stay_separate_identities(self) -> None:
        """A credit journal sits beside the existing draft AR/revenue proposal."""
        ledger, credit_adjustment_id, collection_case_id = record_morning_credit_without_journal()
        invoice_draft_id = ledger.collection_cases[collection_case_id].invoice_draft_id
        credit = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        draft = AccountingExportService(ledger).propose_journal(TENANT_ONE, invoice_draft_id)
        self.assertEqual(credit.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(draft.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertNotEqual(credit.proposal_id, draft.proposal_id)
        self.assertEqual(credit.proposal_lines[0].account_role_code, "usage_revenue")
        self.assertEqual(draft.proposal_lines[0].account_role_code, "accounts_receivable")
        self.assertEqual(len(ledger.journal_proposals), 2)

    def test_taxed_credit_compose_reuses_existing_tax_unwind(self) -> None:
        """Taxed credits keep the #20 three-line unwind and do not add a second journal."""
        ledger = seed_rated_ledger()
        TaxRateService(ledger).publish_tax_rate(TENANT_ONE, "vat", STANDARD_TAX_RATE)
        invoice_draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", HUNDRED)
        TaxAssessmentService(ledger).assess_tax(TENANT_ONE, invoice_draft_id, 1)
        accepted = CreditAdjustmentService(ledger).record_credit_adjustment(
            TENANT_ONE, invoice_draft_id, Decimal("110.00"), "rating_correction"
        )
        if accepted.credit_adjustment_id is None:
            raise AssertionError("taxed credit must persist a credit adjustment")
        prior_count = len(ledger.journal_proposals)
        replay = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, accepted.credit_adjustment_id
        )
        self.assertEqual(
            replay.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(replay.proposal_id, accepted.proposal_id)
        self.assertEqual(len(ledger.journal_proposals), prior_count)
        revenue, payable, receivable = replay.proposal_lines
        self.assertEqual(revenue.account_role_code, "usage_revenue")
        self.assertEqual(revenue.debit_amount, HUNDRED)
        self.assertEqual(payable.account_role_code, "tax_payable")
        self.assertEqual(payable.debit_amount, Decimal("10.00"))
        self.assertEqual(receivable.account_role_code, "accounts_receivable")
        self.assertEqual(receivable.credit_amount, Decimal("110.00"))
        self.assertEqual(
            revenue.debit_amount + payable.debit_amount, receivable.credit_amount
        )

    def test_other_tenant_cannot_see_or_propose_the_first_credit(self) -> None:
        """A tenant cannot propose or list another tenant's credit journal."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal()
        service = AccountingExportService(ledger)
        owned = service.propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        crossed = service.propose_credit_journal(TENANT_TWO, credit_adjustment_id)
        self.assertEqual(owned.proposal_lines[0].debit_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        self.assertEqual(len(one_rows), 1)
        self.assertEqual(one_rows[0].journal_proposal_id, owned.proposal_id)
        self.assertEqual(len(two_rows), 0)

    def test_missing_credit_and_tenant_fail_closed(self) -> None:
        """A credit journal cannot invent money without a stored tenant credit."""
        ledger, _credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal()
        service = AccountingExportService(ledger)
        missing_credit = service.propose_credit_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_credit.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_credit.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND,
        )
        missing_tenant = service.propose_credit_journal(
            "urn:cwl:missing_tenant", generate_record_id()
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_currency_mismatch_and_invalid_amount_fail_closed(self) -> None:
        """Optional currency must match; zero, negative, and IEEE floats refuse."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        service = AccountingExportService(ledger)
        mismatch = service.propose_credit_journal(
            TENANT_ONE, credit_adjustment_id, currency_code="EUR"
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matched = service.propose_credit_journal(
            TENANT_ONE, credit_adjustment_id, currency_code="USD"
        )
        self.assertEqual(matched.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        stored = ledger.get_credit_adjustment(credit_adjustment_id)
        if stored is None:
            raise AssertionError("matched compose must leave the credit stored")
        ledger.credit_adjustments[credit_adjustment_id] = replace(
            stored, credit_amount=Decimal("0")
        )
        zero = AccountingExportService(ledger).propose_credit_journal(
            TENANT_ONE, credit_adjustment_id
        )
        self.assertEqual(zero.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        leftover_ledger, leftover_id, _leftover_case = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        leftover_stored = leftover_ledger.get_credit_adjustment(leftover_id)
        if leftover_stored is None:
            raise AssertionError("heal path must persist the leftover credit")
        leftover_ledger.credit_adjustments[leftover_id] = replace(
            leftover_stored, credit_amount=Decimal("0"), tax_exclusive_amount=Decimal("0")
        )
        rejected_zero = AccountingExportService(leftover_ledger).propose_credit_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_zero.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        leftover_ledger.credit_adjustments[leftover_id] = replace(
            leftover_stored, credit_amount=Decimal("-1"), tax_exclusive_amount=Decimal("-1")
        )
        rejected_negative = AccountingExportService(leftover_ledger).propose_credit_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            rejected_negative.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        leftover_ledger.credit_adjustments[leftover_id] = replace(
            leftover_stored, credit_amount=0.003705  # type: ignore[arg-type]
        )
        floated = AccountingExportService(leftover_ledger).propose_credit_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        leftover_ledger.credit_adjustments[leftover_id] = replace(
            leftover_stored,
            credit_amount=Decimal("1.00"),
            tax_exclusive_amount=Decimal("0.50"),
            tax_amount=Decimal("0"),
        )
        split_mismatch = AccountingExportService(leftover_ledger).propose_credit_journal(
            TENANT_ONE, leftover_id
        )
        self.assertEqual(
            split_mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_AMOUNT_INVALID,
        )
        missing_draft_ledger, missing_draft_id, _missing_case = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        missing_credit = missing_draft_ledger.get_credit_adjustment(missing_draft_id)
        if missing_credit is None:
            raise AssertionError("heal path must persist the missing-draft credit")
        del missing_draft_ledger.invoice_drafts[missing_credit.invoice_draft_id]
        missing_draft = AccountingExportService(missing_draft_ledger).propose_credit_journal(
            TENANT_ONE, missing_draft_id
        )
        self.assertEqual(
            missing_draft.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND,
        )
        crossed_draft_ledger, crossed_draft_id, _crossed_case = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        crossed_credit = crossed_draft_ledger.get_credit_adjustment(crossed_draft_id)
        if crossed_credit is None:
            raise AssertionError("heal path must persist the crossed-draft credit")
        stored_draft = crossed_draft_ledger.get_invoice_draft(crossed_credit.invoice_draft_id)
        if stored_draft is None:
            raise AssertionError("heal path must persist the invoice draft")
        crossed_draft_ledger.invoice_drafts[stored_draft.invoice_draft_id] = replace(
            stored_draft, tenant_account_id=generate_record_id()
        )
        crossed_draft = AccountingExportService(crossed_draft_ledger).propose_credit_journal(
            TENANT_ONE, crossed_draft_id
        )
        self.assertEqual(
            crossed_draft.rejection_reason_code,
            JournalProposalRejectionReasonCode.CREDIT_ADJUSTMENT_NOT_FOUND,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(len(leftover_ledger.journal_proposals), 0)
        self.assertEqual(len(ledger.journal_proposals), 1)

    def test_http_compose_lists_credit_journal_without_a_second_write(self) -> None:
        """POST compose then GET /v1/journal-proposals leaves one validated proposal."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), 0)
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "EUR"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "currency_mismatch")
        self.assertEqual(len(ledger.journal_proposals), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["proposal_status"], "validated")
        self.assertNotEqual(accepted_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", accepted_body)
        self.assertEqual(accepted_body["lines"][0]["account_role_code"], "usage_revenue")
        self.assertEqual(
            accepted_body["lines"][0]["debit_amount"], format_exact_decimal(PARTIAL_CREDIT)
        )
        self.assertEqual(accepted_body["lines"][1]["account_role_code"], "accounts_receivable")
        self.assertEqual(
            accepted_body["lines"][1]["credit_amount"], format_exact_decimal(PARTIAL_CREDIT)
        )
        self.assertEqual(validate_journal_proposal(accepted_body), ())
        proposal_id = accepted_body["proposal_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
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
        credit_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][0]["account_role_code"] == "usage_revenue"
        ]
        self.assertEqual(len(credit_items), 1)
        self.assertEqual(credit_items[0]["proposal_id"], proposal_id)
        self.assertTrue(
            credit_items[0]["idempotency_key"].startswith(
                f"{TENANT_ONE}:credit_adjustment:{credit_adjustment_id}:"
            )
        )
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
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(crossed_status, 422)
        self.assertEqual(crossed_body["rejection_reason_code"], "credit_adjustment_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(len(ledger.journal_proposals), 1)
        self.assertEqual(len(ledger.payment_receipts), 0)
        self.assertEqual(len(ledger.collection_case_settlements), 0)

    def test_resolver_and_ledger_guards_cover_identity(self) -> None:
        """Hollow tenant resolve raises; credit journal rows stay append-only."""
        ledger, credit_adjustment_id, _collection_case_id = record_morning_credit_without_journal(
            PARTIAL_CREDIT
        )
        service = AccountingExportService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        first = service.propose_credit_journal(TENANT_ONE, credit_adjustment_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        credit_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(credit_collision, credit_collision.proposal_lines)
        self.assertEqual(stored.credit_adjustment_id, credit_adjustment_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_credit_adjustment(
                stored.tenant_account_id,
                generate_record_id(),
            )
        )
        found = ledger.find_journal_proposal_for_credit_adjustment(
            stored.tenant_account_id,
            credit_adjustment_id,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)
        empty = AccountingExportService()
        rejected = empty.propose_credit_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency_type_status, currency_type_body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/credit-adjustments/{credit_adjustment_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
