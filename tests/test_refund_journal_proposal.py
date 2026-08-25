"""Refund journal tests for unused parked leftover, replay, and AIS pull."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

from metering_billing import (
    AccountingExportService,
    MemoryUsageLedger,
    UnappliedCashRefundService,
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
from metering_billing.usage_ledger import generate_record_id
from test_http_app import invoke_http
from test_unapplied_cash import LEFTOVER
from test_unapplied_cash_refund import _park_leftover
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


PROPOSED_AT = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)


def record_morning_refund() -> tuple[MemoryUsageLedger, UUID, UUID]:
    """Persist one leftover refund without composing a journal."""
    ledger, parked, _receipt_id = _park_leftover()
    refunded = UnappliedCashRefundService(ledger).refund_unapplied_cash(
        TENANT_ONE, parked.unapplied_cash_id
    )
    if refunded.unapplied_cash_refund_id is None:
        raise AssertionError("known morning path must persist an unapplied cash refund")
    return ledger, refunded.unapplied_cash_refund_id, parked.unapplied_cash_id


class RefundJournalProposalTests(unittest.TestCase):
    """Verify refund exports stay balanced, exact, and proposal-only."""

    def test_known_refund_emits_balanced_unapplied_cash_and_cash_receipt_proposal(
        self,
    ) -> None:
        """A stored leftover refund becomes one balanced unapplied-cash/cash proposal."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        prior_receipts = len(ledger.payment_receipts)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_refunds = len(ledger.unapplied_cash_refunds)
        result = AccountingExportService(
            ledger, clock=lambda: PROPOSED_AT
        ).propose_refund_journal(TENANT_ONE, refund_id)
        self.assertEqual(result.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertIsInstance(result.proposal_id, UUID)
        self.assertEqual(result.proposal_status, "validated")
        self.assertNotEqual(result.proposal_status, "posted")
        self.assertEqual(result.transaction_currency, "USD")
        self.assertEqual(result.intended_book_role_code, "primary_statutory")
        self.assertEqual(result.unapplied_cash_refund_id, refund_id)
        self.assertEqual(
            result.source_event_references,
            (f"{TENANT_ONE}:unapplied_cash_refund:{refund_id}",),
        )
        self.assertEqual(
            result.idempotency_key,
            (
                f"{TENANT_ONE}:unapplied_cash_refund:{refund_id}:"
                f"{result.source_payload_hash}:v{result.proposal_contract_version}"
            ),
        )
        self.assertEqual(len(result.proposal_lines), 2)
        debit, credit = result.proposal_lines
        self.assertEqual(debit.account_role_code, "unapplied_cash")
        self.assertEqual(debit.debit_amount, LEFTOVER)
        self.assertEqual(debit.credit_amount, Decimal("0"))
        self.assertEqual(credit.account_role_code, "cash_receipt")
        self.assertEqual(credit.debit_amount, Decimal("0"))
        self.assertEqual(credit.credit_amount, LEFTOVER)
        self.assertEqual(debit.debit_amount + credit.debit_amount, credit.credit_amount)
        self.assertNotIsInstance(debit.debit_amount, float)
        payload = result.as_contract_dict()
        self.assertEqual(validate_journal_proposal(payload), ())
        self.assertNotIn("journal_proposal_outcome_code", payload)
        self.assertNotIn("statutory_account_id", payload)
        self.assertNotIn("chart_account_id", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id == refund_id
                ]
            ),
            1,
        )
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.unapplied_cash_refunds), prior_refunds)
        self.assertEqual(len(ledger.accounting_export_records), 0)

    def test_refund_accept_does_not_compose_a_journal(self) -> None:
        """#57 refund stays without a journal until the explicit compose command."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        prior_journals = len(
            [
                proposal
                for proposal in ledger.journal_proposals.values()
                if proposal.unapplied_cash_refund_id is not None
            ]
        )
        self.assertEqual(prior_journals, 0)
        first = AccountingExportService(ledger).propose_refund_journal(TENANT_ONE, refund_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id is not None
                ]
            ),
            1,
        )

    def test_second_propose_of_the_same_refund_is_a_replay(self) -> None:
        """The same tenant and refund reuse proposal_id and do not grow the store."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        service = AccountingExportService(ledger)
        first = service.propose_refund_journal(TENANT_ONE, refund_id)
        second = service.propose_refund_journal(TENANT_ONE, refund_id)
        self.assertEqual(first.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(second.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(second.proposal_id, first.proposal_id)
        self.assertEqual(second.source_payload_hash, first.source_payload_hash)
        self.assertEqual(second.proposal_lines[0].debit_amount, LEFTOVER)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id is not None
                ]
            ),
            1,
        )
        self.assertEqual(validate_journal_proposal(second.as_contract_dict()), ())

    def test_invoice_and_cash_journals_stay_separate_identities(self) -> None:
        """A refund journal sits beside the existing cash and draft proposals."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        refund = AccountingExportService(ledger).propose_refund_journal(TENANT_ONE, refund_id)
        stored_refund = ledger.get_unapplied_cash_refund(refund_id)
        self.assertIsNotNone(stored_refund)
        collection_case = ledger.get_collection_case(stored_refund.collection_case_id)
        self.assertIsNotNone(collection_case)
        draft = AccountingExportService(ledger).propose_journal(
            TENANT_ONE, collection_case.invoice_draft_id
        )
        self.assertEqual(refund.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertEqual(draft.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        self.assertNotEqual(refund.proposal_id, draft.proposal_id)
        self.assertEqual(refund.proposal_lines[0].account_role_code, "unapplied_cash")
        self.assertEqual(refund.proposal_lines[1].account_role_code, "cash_receipt")
        self.assertEqual(draft.proposal_lines[0].account_role_code, "accounts_receivable")

    def test_other_tenant_cannot_see_or_propose_the_first_refund(self) -> None:
        """A tenant cannot propose or list another tenant's refund journal."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        service = AccountingExportService(ledger)
        owned = service.propose_refund_journal(TENANT_ONE, refund_id)
        crossed = service.propose_refund_journal(TENANT_TWO, refund_id)
        self.assertEqual(owned.proposal_lines[0].debit_amount, LEFTOVER)
        self.assertEqual(crossed.journal_proposal_outcome_code, JournalProposalOutcomeCode.REJECTED)
        self.assertEqual(
            crossed.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_REFUND_NOT_FOUND,
        )
        self.assertNotIn("proposal_id", crossed.as_contract_dict())
        one_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_ONE).tenant_account_id)
        two_rows = ledger.list_journal_proposals(ledger.require_tenant(TENANT_TWO).tenant_account_id)
        refund_rows = [row for row in one_rows if row.unapplied_cash_refund_id == refund_id]
        self.assertEqual(len(refund_rows), 1)
        self.assertEqual(refund_rows[0].journal_proposal_id, owned.proposal_id)
        self.assertEqual(len(two_rows), 0)

    def test_missing_refund_and_tenant_fail_closed(self) -> None:
        """A refund journal cannot invent money without a stored tenant refund."""
        ledger, _refund_id, _leftover_id = record_morning_refund()
        service = AccountingExportService(ledger)
        missing_refund = service.propose_refund_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_refund.journal_proposal_outcome_code,
            JournalProposalOutcomeCode.REJECTED,
        )
        self.assertEqual(
            missing_refund.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_REFUND_NOT_FOUND,
        )
        missing_tenant = service.propose_refund_journal(
            "urn:cwl:missing_tenant", generate_record_id()
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )

    def test_currency_mismatch_and_invalid_amount_fail_closed(self) -> None:
        """Optional currency must match; zero, negative, and IEEE floats refuse."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        service = AccountingExportService(ledger)
        mismatch = service.propose_refund_journal(
            TENANT_ONE, refund_id, currency_code="EUR"
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            JournalProposalRejectionReasonCode.CURRENCY_MISMATCH,
        )
        matched = service.propose_refund_journal(
            TENANT_ONE, refund_id, currency_code="USD"
        )
        self.assertEqual(matched.journal_proposal_outcome_code, JournalProposalOutcomeCode.ACCEPTED)
        stored = ledger.get_unapplied_cash_refund(refund_id)
        ledger.unapplied_cash_refunds[refund_id] = replace(
            stored, refund_amount=Decimal("0")
        )
        zero = AccountingExportService(ledger).propose_refund_journal(TENANT_ONE, refund_id)
        self.assertEqual(zero.journal_proposal_outcome_code, JournalProposalOutcomeCode.DUPLICATE_REPLAY)
        leftover_ledger, leftover_refund_id, _leftover = record_morning_refund()
        leftover_stored = leftover_ledger.get_unapplied_cash_refund(leftover_refund_id)
        leftover_ledger.unapplied_cash_refunds[leftover_refund_id] = replace(
            leftover_stored, refund_amount=Decimal("0")
        )
        rejected_zero = AccountingExportService(leftover_ledger).propose_refund_journal(
            TENANT_ONE, leftover_refund_id
        )
        self.assertEqual(
            rejected_zero.rejection_reason_code,
            JournalProposalRejectionReasonCode.REFUND_AMOUNT_INVALID,
        )
        leftover_ledger.unapplied_cash_refunds[leftover_refund_id] = replace(
            leftover_stored, refund_amount=Decimal("-1")
        )
        rejected_negative = AccountingExportService(leftover_ledger).propose_refund_journal(
            TENANT_ONE, leftover_refund_id
        )
        self.assertEqual(
            rejected_negative.rejection_reason_code,
            JournalProposalRejectionReasonCode.REFUND_AMOUNT_INVALID,
        )
        leftover_ledger.unapplied_cash_refunds[leftover_refund_id] = replace(
            leftover_stored, refund_amount=0.003705  # type: ignore[arg-type]
        )
        floated = AccountingExportService(leftover_ledger).propose_refund_journal(
            TENANT_ONE, leftover_refund_id
        )
        self.assertEqual(
            floated.rejection_reason_code,
            JournalProposalRejectionReasonCode.REFUND_AMOUNT_INVALID,
        )
        with self.assertRaises(ExactDecimalError):
            parse_proposal_amount(0.003705)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in leftover_ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id is not None
                ]
            ),
            0,
        )

    def test_http_compose_lists_refund_journal_without_a_second_write(self) -> None:
        """POST compose then GET /v1/journal-proposals leaves one validated proposal."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        prior_refund_journals = len(
            [
                proposal
                for proposal in ledger.journal_proposals.values()
                if proposal.unapplied_cash_refund_id is not None
            ]
        )
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id is not None
                ]
            ),
            prior_refund_journals,
        )
        mismatch_status, mismatch_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "EUR"},
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "currency_mismatch")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": "USD"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["proposal_status"], "validated")
        self.assertNotEqual(accepted_body["proposal_status"], "posted")
        self.assertNotIn("journal_proposal_outcome_code", accepted_body)
        self.assertEqual(accepted_body["lines"][0]["account_role_code"], "unapplied_cash")
        self.assertEqual(accepted_body["lines"][0]["debit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(accepted_body["lines"][1]["account_role_code"], "cash_receipt")
        self.assertEqual(accepted_body["lines"][1]["credit_amount"], format_exact_decimal(LEFTOVER))
        self.assertEqual(validate_journal_proposal(accepted_body), ())
        proposal_id = accepted_body["proposal_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
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
        refund_items = [
            item
            for item in list_body["journal_proposals"]
            if item["lines"][0]["account_role_code"] == "unapplied_cash"
        ]
        self.assertEqual(len(refund_items), 1)
        self.assertEqual(refund_items[0]["proposal_id"], proposal_id)
        self.assertTrue(
            refund_items[0]["idempotency_key"].startswith(
                f"{TENANT_ONE}:unapplied_cash_refund:{refund_id}:"
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
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(crossed_status, 422)
        self.assertEqual(crossed_body["rejection_reason_code"], "unapplied_cash_refund_not_found")
        method_status, _method_body = invoke_http(
            app,
            "PUT",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 405)
        self.assertEqual(
            len(
                [
                    proposal
                    for proposal in ledger.journal_proposals.values()
                    if proposal.unapplied_cash_refund_id is not None
                ]
            ),
            1,
        )

    def test_resolver_and_ledger_guards_cover_identity(self) -> None:
        """Hollow tenant resolve raises; refund journal rows stay append-only."""
        ledger, refund_id, _leftover_id = record_morning_refund()
        service = AccountingExportService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.propose_refund_journal(TENANT_ONE, refund_id)
        first = service.propose_refund_journal(TENANT_ONE, refund_id)
        stored = ledger.journal_proposals[first.proposal_id]
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(stored, stored.proposal_lines)
        colliding = replace(stored, journal_proposal_id=generate_record_id())
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(colliding, colliding.proposal_lines)
        refund_collision = replace(
            stored,
            journal_proposal_id=generate_record_id(),
            invoice_draft_id=generate_record_id(),
            source_payload_hash="sha256:" + "a" * 64,
        )
        with self.assertRaises(ValueError):
            ledger.insert_journal_proposal(refund_collision, refund_collision.proposal_lines)
        self.assertEqual(stored.unapplied_cash_refund_id, refund_id)
        self.assertIsNone(
            ledger.find_journal_proposal_for_refund(
                stored.tenant_account_id,
                generate_record_id(),
            )
        )
        found = ledger.find_journal_proposal_for_refund(
            stored.tenant_account_id,
            refund_id,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.journal_proposal_id, first.proposal_id)
        empty = AccountingExportService()
        rejected = empty.propose_refund_journal(TENANT_ONE, generate_record_id())
        self.assertEqual(
            rejected.rejection_reason_code,
            JournalProposalRejectionReasonCode.TENANT_NOT_FOUND,
        )
        currency_type_status, currency_type_body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/unapplied-cash-refunds/{refund_id}/journal-proposals",
            {"tenant_reference": TENANT_ONE, "currency_code": 1},
        )
        self.assertEqual(currency_type_status, 422)
        self.assertEqual(currency_type_body["rejection_reason_code"], "request_invalid")
        orphan_ledger, orphan_refund_id, _orphan_leftover = record_morning_refund()
        orphan_stored = orphan_ledger.get_unapplied_cash_refund(orphan_refund_id)
        del orphan_ledger.collection_cases[orphan_stored.collection_case_id]
        missing_case = AccountingExportService(orphan_ledger).propose_refund_journal(
            TENANT_ONE, orphan_refund_id
        )
        self.assertEqual(
            missing_case.rejection_reason_code,
            JournalProposalRejectionReasonCode.UNAPPLIED_CASH_REFUND_NOT_FOUND,
        )


if __name__ == "__main__":
    unittest.main()
